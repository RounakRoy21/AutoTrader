#!/bin/bash
# startup.sh — start all AutoTrader services (called by scheduler at 05:30 IST)
# Run inside Ubuntu (proot-distro login ubuntu)
set -euo pipefail

LOGFILE=/root/autotrader/logs/startup.log
BACKEND_DIR=/root/autotrader/trading-system/backend
BACKEND_LOG=/root/autotrader/logs/backend.log
PID_FILE=/tmp/autotrader-backend.pid

mkdir -p /root/autotrader/logs

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg" | tee -a "$LOGFILE"
}

fail() {
    log "ERROR: $*"
    # Send a Telegram alert about startup failure using the backend's .env
    ENV_FILE="$BACKEND_DIR/../.env"
    if [ -f "$ENV_FILE" ]; then
        TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
        CHAT=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)
        if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
            curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
                -d chat_id="$CHAT" \
                -d text="⚠️ AutoTrader startup FAILED on $(date '+%Y-%m-%d'): $*" \
                -d parse_mode=HTML > /dev/null || true
        fi
    fi
    exit 1
}

log "=============================="
log " AutoTrader startup"
log "=============================="

# ── Redis ────────────────────────────────────────────────────────────────────
if redis-cli -s /tmp/redis.sock ping &>/dev/null 2>&1 || redis-cli ping &>/dev/null 2>&1; then
    log "Redis: already running"
else
    log "Redis: starting..."
    redis-server --daemonize yes \
                 --logfile /root/autotrader/logs/redis.log \
                 --save 900 1 --save 300 10
    sleep 1
    redis-cli ping | grep -q PONG || fail "Redis did not start"
    log "Redis: OK"
fi

# ── PostgreSQL ───────────────────────────────────────────────────────────────
PG_VER=$(pg_lsclusters --no-header 2>/dev/null | awk '{print $1; exit}')
if [ -z "$PG_VER" ]; then
    fail "No PostgreSQL cluster found. Did install.sh complete successfully?"
fi

PG_STATUS=$(pg_ctlcluster "$PG_VER" main status 2>&1 || true)
if echo "$PG_STATUS" | grep -q "online"; then
    log "PostgreSQL $PG_VER: already running"
else
    log "PostgreSQL $PG_VER: starting..."
    pg_ctlcluster "$PG_VER" main start
    sleep 2
    pg_ctlcluster "$PG_VER" main status | grep -q "online" || fail "PostgreSQL did not start"
    log "PostgreSQL $PG_VER: OK"
fi

# ── nginx ─────────────────────────────────────────────────────────────────────
if pgrep -x nginx > /dev/null 2>&1; then
    log "nginx: already running"
else
    log "nginx: starting..."
    nginx -c /root/autotrader/trading-system/scripts/android/nginx.conf
    sleep 1
    pgrep -x nginx > /dev/null 2>&1 || fail "nginx did not start"
    log "nginx: OK (dashboard at http://localhost:4201)"
fi

# ── Backend ───────────────────────────────────────────────────────────────────
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    log "Backend: already running (pid $(cat "$PID_FILE"))"
else
    # Clean up stale PID file
    rm -f "$PID_FILE"

    if pgrep -f "uvicorn main:app" > /dev/null 2>&1; then
        log "Backend: already running (found by name, no PID file)"
    else
        log "Backend: starting..."
        cd "$BACKEND_DIR"
        source .venv/bin/activate

        nohup python -m uvicorn main:app \
            --host 0.0.0.0 \
            --port 8000 \
            --log-level info \
            >> "$BACKEND_LOG" 2>&1 &

        echo $! > "$PID_FILE"
        sleep 4

        # Verify it came up
        for attempt in 1 2 3 4 5; do
            if curl -s http://localhost:8000/api/health > /dev/null 2>&1; then
                log "Backend: OK (pid $(cat "$PID_FILE"))"
                break
            fi
            if [ "$attempt" -eq 5 ]; then
                fail "Backend did not respond after 15s — check $BACKEND_LOG"
            fi
            log "Backend: waiting for health check (attempt $attempt)..."
            sleep 3
        done
    fi
fi

log "=============================="
log " All services running"
log " Dashboard : http://localhost:4201"
log " API docs  : http://localhost:8000/docs"
log "=============================="
