# AutoTrader — OnePlus Nord (Android) Setup Guide

Run the full trading system (backend + frontend) on your spare Android phone using Termux. No root needed. After a one-time setup, everything starts and stops automatically on every trading day.

---

## How It Works

```
Termux (always open, plugged in)
  └── tmux session "autotrader"
        ├── window: scheduler   ← Python script, sleeps until 05:30 / 16:00 IST
        ├── window: backend-log ← live tail of uvicorn output
        └── window: shell       ← manual Ubuntu access

Inside Ubuntu (proot):
  05:30 IST on each trading day  →  startup.sh
    ├── Redis (if not running)
    ├── PostgreSQL (if not running)
    ├── nginx → serves frontend at :4201
    └── uvicorn → backend at :8000

  16:00 IST on each trading day  →  shutdown.sh
    └── stops uvicorn gracefully
        (Redis + PostgreSQL stay up — no need to restart a DB every day)

NSE holidays and weekends: scheduler wakes, logs "not a trading day", goes back to sleep.
```

You only ever touch the phone to: run `install.sh` once, and run `bootstrap.sh` every time you need to re-attach to the tmux session.

---

## Part 1 — One-Time Setup

### Prerequisites

| Item | Action |
|------|--------|
| Termux | Install from **F-Droid** (not Play Store — that version is obsolete). https://f-droid.org |
| Termux:API | Install from F-Droid alongside Termux |
| Power | Keep the phone plugged into a charger throughout setup |

### 1.1 — Configure Termux

Open Termux and run:

```bash
# Update Termux
pkg update && pkg upgrade -y

# Install proot-distro, tmux, and the Termux API bridge
pkg install proot-distro tmux termux-api -y

# Prevent Android from killing Termux in the background
termux-wake-lock
```

Go to **Settings → Battery → Battery Optimisation** → find Termux → set to **Not optimised**. Do the same for Termux:API if it appears in the list.

### 1.2 — Install Ubuntu

```bash
proot-distro install ubuntu
proot-distro login ubuntu
```

You're now inside Ubuntu. **All commands from here until Section 1.4 run inside this Ubuntu shell.**

### 1.3 — Run the Install Script

```bash
# Install everything: packages, PostgreSQL, Redis, clone repo, build frontend, configure nginx
bash <(curl -fsSL https://raw.githubusercontent.com/<your-org>/<your-repo>/main/trading-system/scripts/android/install.sh) \
     <your-git-repo-url>
```

If you can't reach raw.githubusercontent.com, clone manually and run it:

```bash
apt install git -y
git clone <your-git-repo-url> /root/autotrader
bash /root/autotrader/trading-system/scripts/android/install.sh <your-git-repo-url>
```

The script pauses mid-way to let you fill in your `.env` file. When it prompts:

```bash
nano /root/autotrader/trading-system/.env
```

Fill in:

```env
# These already set correctly by install.sh — don't change them:
DATABASE_URL=postgresql+asyncpg://autotrader:changeme_postgres_password@localhost/autotrader
REDIS_URL=redis://localhost:6379

# Fill these in:
GROWW_CLIENT_ID=your_groww_client_id
GROWW_PASSWORD=your_groww_password
GROWW_TOTP_SECRET=your_base32_totp_secret
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC-...
TELEGRAM_CHAT_ID=your_chat_id

# Leave as true until you're confident:
PAPER_TRADING=true
ADMIN_API_KEY=change_me_to_something_random
```

Save (`Ctrl+O`, Enter, `Ctrl+X`), then press Enter in the installer to continue.

### 1.4 — Exit Ubuntu and Run the Bootstrap

When install.sh completes, exit Ubuntu:

```bash
exit
```

You're back in Termux. Run:

```bash
bash /root/autotrader/trading-system/scripts/android/bootstrap.sh
```

This creates the tmux session with three windows and starts the scheduler. You'll see:

```
Session created. Windows:
  0 scheduler   — the automatic daily scheduler
  1 backend-log — live uvicorn output
  2 shell        — manual Ubuntu shell
```

The scheduler logs its next planned event and then sleeps:

```
2026-05-04 10:32:15  INFO  Next: startup    on Mon 2026-05-05 at 05:30 IST  (68700 s away)
```

**Setup is complete.** You don't need to do anything else.

---

## Part 2 — Daily Behaviour (Automatic)

| Time | What happens |
|------|-------------|
| 05:30 IST Mon–Fri (trading days) | startup.sh runs: starts Redis, PostgreSQL, nginx, uvicorn |
| 06:00 IST | Research Agent fires (inside the backend) |
| 09:15 IST | Trading begins |
| 15:30 IST | EOD report generated, Telegram message sent |
| 16:00 IST | shutdown.sh runs: stops uvicorn gracefully |
| Weekends / NSE holidays | Scheduler wakes, logs the skip, sleeps again |

You receive all alerts on Telegram — no interaction with the phone needed.

---

## Part 3 — Accessing the Dashboard

### Find the phone's IP address

In the tmux `shell` window (window 2), inside Ubuntu:

```bash
ip addr show wlan0 | grep "inet "
# e.g.: inet 192.168.1.105/24
```

Or in Termux (outside Ubuntu):

```bash
ifconfig wlan0 | grep "inet "
```

### URLs (after 05:30 on a trading day, when backend is running)

| URL | What it is |
|-----|------------|
| `http://192.168.1.105:4201` | Trading dashboard |
| `http://192.168.1.105:8000/docs` | Backend API explorer |

Both are accessible from any device on the same WiFi.

---

## Part 4 — Operating the tmux Session

### Re-attaching after you close Termux

```bash
# In Termux:
bash /root/autotrader/trading-system/scripts/android/bootstrap.sh
# If the session is already running, this just re-attaches.
```

Or directly:

```bash
tmux attach -t autotrader
```

### tmux cheatsheet

| Keys | Action |
|------|--------|
| `Ctrl-B 0` | Switch to scheduler window |
| `Ctrl-B 1` | Switch to backend-log window |
| `Ctrl-B 2` | Switch to shell window |
| `Ctrl-B D` | Detach (session keeps running) |
| `Ctrl-B [` | Scroll mode (use arrow keys, `q` to exit) |

### Manually triggering startup or shutdown

In the tmux `shell` window (window 2, inside Ubuntu):

```bash
bash /root/autotrader/trading-system/scripts/android/startup.sh
# or
bash /root/autotrader/trading-system/scripts/android/shutdown.sh
```

### Checking logs

```bash
# Scheduler decisions (what it skipped / triggered)
tail -f /root/autotrader/logs/scheduler.log

# uvicorn / trading engine output
tail -f /root/autotrader/logs/backend.log

# Today's startup log
cat /root/autotrader/logs/startup.log

# Today's shutdown log
cat /root/autotrader/logs/shutdown.log
```

---

## Part 5 — Keeping Android From Killing Termux

Android will eventually kill background processes despite the wake lock. Do all three of these:

1. **Battery optimisation off**: Settings → Battery → Battery Optimisation → Termux → Not optimised
2. **Wake lock active**: run `termux-wake-lock` in Termux before starting bootstrap
3. **Keep the screen locked but plugged in**: a locked screen doesn't kill Termux; it only dies if you swipe it away from Recents

If Termux does get killed (check: open Termux and you see a fresh prompt instead of the tmux session), just re-run bootstrap:

```bash
termux-wake-lock
bash /root/autotrader/trading-system/scripts/android/bootstrap.sh
```

The scheduler will re-compute the next event from the current time and carry on.

---

## Part 6 — Updating the App

When you push new code:

```bash
# In the tmux shell window (window 2, Ubuntu):
cd /root/autotrader
git pull

cd trading-system/backend
source .venv/bin/activate
pip install -r requirements.txt --quiet
alembic upgrade head

# Rebuild frontend if you changed frontend code
cd ../frontend
npm install --quiet
npx ng build --configuration production
cp -r dist/autotrader-dashboard/browser/* /var/www/autotrader/

# Restart the backend to pick up code changes
bash /root/autotrader/trading-system/scripts/android/shutdown.sh
bash /root/autotrader/trading-system/scripts/android/startup.sh
```

---

## Part 7 — Updating NSE Holidays

The NSE holiday calendar lives in the backend:

```
trading-system/backend/core/nse_calendar.py
```

Each December, update `NSE_HOLIDAYS_20XX` with the official list from https://www.nseindia.com/resources/exchange-communication-holidays, then `git pull` on the phone.

---

## Troubleshooting

**Scheduler window shows "not a trading day" when it should have started**
Check the date on the phone: `date` in Ubuntu. If the date is wrong (can happen after a reboot), restart Termux and retry.

**Backend started but Telegram alerts aren't arriving**
`nano /root/autotrader/trading-system/.env` — verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Send `/start` to your bot in Telegram once if you haven't already.

**nginx started but dashboard is blank**
The frontend files may not be in `/var/www/autotrader/`. Check: `ls /var/www/autotrader/`. If empty, re-run the frontend copy step in Part 6.

**PostgreSQL "cluster not found" error**
`pg_lsclusters` to see the actual version number, then update the `PG_VER` auto-detection if needed.

**`uvicorn` or `pgrep` not found**
You're in Termux, not Ubuntu. Run `proot-distro login ubuntu` first.

**Phone rebooted overnight**
After reboot: open Termux, run `termux-wake-lock`, then `bash ~/autotrader/trading-system/scripts/android/bootstrap.sh`. The scheduler will resume from the correct next event.

