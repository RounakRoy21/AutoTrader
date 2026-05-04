#!/data/data/com.termux/files/usr/bin/bash
# bootstrap.sh — run in Termux (NOT inside Ubuntu) to set up or re-attach the
# AutoTrader tmux session.
#
# First run:  creates the session with three named windows and starts the scheduler
# Later runs: just re-attaches to the existing session
#
# Usage (run from Termux):
#   bash ~/autotrader/trading-system/scripts/android/bootstrap.sh

SESSION="autotrader"

# ─────────────────────────────────────────────────────────────────────────────
# If the session already exists, just re-attach.
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already running — re-attaching..."
    exec tmux attach -t "$SESSION"
fi

echo "Creating tmux session '$SESSION'..."

# ── Window 1: scheduler ───────────────────────────────────────────────────────
# The scheduler runs inside Ubuntu and never exits.
# If it ever crashes, pressing Up+Enter restarts it from the same window.
tmux new-session -d -s "$SESSION" -n "scheduler" \
    "proot-distro login ubuntu -- \
        python3 /root/autotrader/trading-system/scripts/android/scheduler.py ; \
        echo '--- scheduler exited (press Up + Enter to restart) ---' ; \
        bash"

# ── Window 2: backend log ─────────────────────────────────────────────────────
# Live tail of the uvicorn output.  Starts empty until startup.sh fires.
tmux new-window -t "$SESSION" -n "backend-log" \
    "proot-distro login ubuntu -- \
        bash -c 'echo Waiting for backend log...; \
                 while [ ! -f /root/autotrader/logs/backend.log ]; do sleep 2; done; \
                 tail -f /root/autotrader/logs/backend.log'"

# ── Window 3: manual shell ────────────────────────────────────────────────────
# A plain Ubuntu shell for running commands manually (check status, edit .env, etc.)
tmux new-window -t "$SESSION" -n "shell" \
    "proot-distro login ubuntu"

# Focus the scheduler window on attach
tmux select-window -t "$SESSION:scheduler"

echo "Session created. Windows:"
echo "  0 scheduler   — the automatic daily scheduler"
echo "  1 backend-log — live uvicorn output"
echo "  2 shell        — manual Ubuntu shell"
echo
echo "Switch windows inside tmux: Ctrl-B, then 0 / 1 / 2"
echo "Detach (keep running):       Ctrl-B, then D"
echo

exec tmux attach -t "$SESSION"
