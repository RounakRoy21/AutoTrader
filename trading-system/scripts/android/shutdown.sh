#!/bin/bash
# shutdown.sh — gracefully stop the AutoTrader backend (called by scheduler at 16:00 IST)
# Redis and PostgreSQL are left running — no need to restart a database daily.
# Run inside Ubuntu (proot-distro login ubuntu)

LOGFILE=/root/autotrader/logs/shutdown.log
PID_FILE=/tmp/autotrader-backend.pid
BACKEND_LOG=/root/autotrader/logs/backend.log

mkdir -p /root/autotrader/logs

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg" | tee -a "$LOGFILE"
}

log "=============================="
log " AutoTrader shutdown"
log "=============================="

# ── Backend — graceful SIGTERM, SIGKILL fallback after 15s ───────────────────
stop_backend() {
    local pid=""

    # Prefer the PID file
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if ! kill -0 "$pid" 2>/dev/null; then
            log "Backend: PID file exists but process $pid is gone"
            rm -f "$PID_FILE"
            pid=""
        fi
    fi

    # Fall back to finding by name
    if [ -z "$pid" ]; then
        pid=$(pgrep -f "uvicorn main:app" | head -1 || true)
    fi

    if [ -z "$pid" ]; then
        log "Backend: not running"
        return
    fi

    log "Backend: sending SIGTERM to pid $pid..."
    kill -TERM "$pid" 2>/dev/null || true

    # Wait up to 15 seconds for clean shutdown
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ $waited -lt 15 ]; do
        sleep 1
        (( waited++ )) || true
    done

    if kill -0 "$pid" 2>/dev/null; then
        log "Backend: still alive after ${waited}s — sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi

    rm -f "$PID_FILE"
    log "Backend: stopped"
}

stop_backend

# ── nginx — stop so no dangling connections sit open overnight ────────────────
if pgrep -x nginx > /dev/null 2>&1; then
    log "nginx: stopping..."
    nginx -s quit 2>/dev/null || pkill -x nginx || true
    sleep 1
    log "nginx: stopped"
else
    log "nginx: not running"
fi

# ── Tail last 20 lines of today's backend log for quick review ───────────────
log "────────────────────────────────"
log "Backend log tail:"
tail -n 20 "$BACKEND_LOG" 2>/dev/null | while IFS= read -r line; do
    log "  $line"
done

log "=============================="
log " Shutdown complete"
log " Redis and PostgreSQL left running (data safe)"
log " Check Telegram for the EOD report"
log "=============================="
