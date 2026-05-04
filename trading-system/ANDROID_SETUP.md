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
| Storage | The setup downloads around 2–3 GB. Make sure the phone has at least 4 GB of free space. |

> **Installing from F-Droid for the first time?** Because F-Droid isn't from the Play Store, Android will initially block it. When you open the downloaded F-Droid APK you'll get a warning like *"For your security, your phone is not allowed to install unknown apps."* To fix this: go to **Settings → Apps → Special App Access → Install unknown apps**, find your browser (e.g. Chrome), and switch it to **Allowed**. Then go back and open the APK again. This is a one-time step.

### 1.1 — Configure Termux

Open Termux and run:

> **Keyboard tip:** On most Android keyboards, there is no `Ctrl` key. In Termux, the **Volume Down** button acts as `Ctrl`. So to cancel a running command (`Ctrl+C`) press Volume Down + C. Some keyboards also show an `ESC` key in an extra row — Termux may offer this as an option in its settings.

```bash
# Download the latest versions of Termux's own software tools
# (It will ask "Do you want to continue? [Y/n]" — type Y and press Enter)
pkg update && pkg upgrade -y

# Install three tools:
#   proot-distro — lets you run Ubuntu (a full Linux system) inside your Android phone
#   tmux         — a terminal multiplexer: runs multiple windows in one session,
#                  and keeps everything alive even when you detach or close the app
#   termux-api   — a bridge between Termux and Android system features
pkg install proot-distro tmux termux-api -y

# Tell Android not to put Termux to sleep in the background
termux-wake-lock
```

Go to **Settings → Battery → Battery Optimisation** → find Termux → set to **Not optimised**. Do the same for Termux:API if it appears. This tells Android "never kill this app to save battery."

### 1.2 — Install Ubuntu

```bash
# Download and install Ubuntu inside your phone.
# Ubuntu is a free Linux operating system — it gives us a proper environment
# to run databases, a web server, and Python reliably.
# This download is about 200 MB and may take a few minutes.
proot-distro install ubuntu

# Enter Ubuntu. Your prompt will change — that's expected.
proot-distro login ubuntu
```

After `proot-distro login ubuntu`, your prompt changes from `$` to something like:

```
root@localhost:~#
```

That means you are now **inside Ubuntu**. Think of it as stepping into a separate operating system running inside your phone. **All commands from here until Section 1.4 run inside this Ubuntu shell.** If at any point you need to get back to Termux, just type `exit` and press Enter.

### 1.3 — Clone the Repo and Run the Install Script

```bash
apt install git -y
git clone https://github.com/RounakRoy21/AutoTrader.git /root/autotrader
bash /root/autotrader/trading-system/scripts/android/install.sh https://github.com/RounakRoy21/AutoTrader.git
```

> **This takes 10–20 minutes.** The script installs packages, sets up a database, installs Node.js, and builds the frontend. The phone will look busy for long stretches — that is completely normal. Do not close Termux.

Mid-way through, the installer pauses and asks you to fill in your configuration file. It will print:

```
ACTION REQUIRED: edit /root/autotrader/trading-system/.env and fill in your credentials.
Press Enter when done...
```

At that point, open the file with:

```bash
nano /root/autotrader/trading-system/.env
```

`nano` is a basic text editor built into the terminal. Use your arrow keys to move around. To save and exit: press **Ctrl+O** (Volume Down + O on Android), then **Enter** to confirm the filename, then **Ctrl+X** to close.

The file already has the database and Redis URLs filled in correctly. Fill in the rest:

```env
# Already set correctly — do not change:
DATABASE_URL=postgresql+asyncpg://autotrader:changeme_postgres_password@localhost/autotrader
REDIS_URL=redis://localhost:6379

# Your credentials:
GROWW_CLIENT_ID=your_groww_client_id
GROWW_PASSWORD=your_groww_password
GROWW_TOTP_SECRET=your_base32_totp_secret
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC-...
TELEGRAM_CHAT_ID=your_chat_id

# Leave as true until you're confident in live trading:
PAPER_TRADING=true
ADMIN_API_KEY=change_me_to_something_random
```

**What each setting means and where to get it:**

| Setting | What it is | Where to get it |
|---------|-----------|----------------|
| `GROWW_CLIENT_ID` | Your Groww login username or client ID | Your Groww account |
| `GROWW_PASSWORD` | Your Groww account password | Your Groww account |
| `GROWW_TOTP_SECRET` | The secret key behind Groww's two-factor authentication (2FA). This is **not** the 6-digit code you type — it's the underlying key that *generates* those codes. It looks like `JBSWY3DPEHPK3PXP`. | When you set up 2FA on Groww, you scanned a QR code with an app like Google Authenticator. The text version of that QR code is this secret. Check your authenticator app's export/backup option, or re-do the Groww 2FA setup and look for "show key" or "copy key" during the QR step. |
| `ANTHROPIC_API_KEY` | API key for Claude AI, which the system uses to analyse stocks and make decisions | Go to https://console.anthropic.com → sign in → **API Keys** → **Create Key** |
| `TELEGRAM_BOT_TOKEN` | Identifies your Telegram bot (it sends you trade alerts) | Open Telegram → search **@BotFather** → send `/newbot` → follow the prompts → copy the token it gives you (format: `123456789:ABC...`) |
| `TELEGRAM_CHAT_ID` | Your personal Telegram user ID, so the bot knows who to message | Open Telegram → search **@userinfobot** → send any message → it replies with your ID (a plain number like `987654321`) |
| `PAPER_TRADING` | `true` = no real orders are placed, safe for testing. `false` = live trading with real money. **Leave this as `true` until you have verified the system is working correctly.** | — |
| `ADMIN_API_KEY` | A password to protect the web dashboard. Anyone who knows this can access the dashboard. | Make up any string (e.g. `mysecret123`) |

Save the file (Ctrl+O → Enter → Ctrl+X), then press **Enter** in the terminal to let the installer continue.

### 1.4 — Exit Ubuntu and Run the Bootstrap

When install.sh completes, exit Ubuntu:

```bash
exit
```

You're back in Termux. The installer has already placed a launcher script in your home folder. Run:

```bash
bash ~/start-autotrader.sh
```

> **If you see "No such file or directory":** The automatic copy may have failed. Use this fallback instead:
> ```bash
> proot-distro login ubuntu -- bash /root/autotrader/trading-system/scripts/android/bootstrap.sh
> ```

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
bash ~/start-autotrader.sh
# If the session is already running, this just re-attaches to it.
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
bash ~/start-autotrader.sh
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
After reboot: open Termux, run `termux-wake-lock`, then `bash ~/start-autotrader.sh`. The scheduler will re-compute the next event from the current time and carry on.

**The install script failed partway through**
That's fine — `install.sh` is safe to re-run. It skips steps that are already done (it won't re-download packages that are installed, won't recreate the database if it exists, etc.). Just run the same `bash install.sh` command again from inside Ubuntu.

**"E: Unable to fetch some archives" or "Failed to fetch" during install**
This is a network error — your WiFi connection dropped or the package server was temporarily busy. Make sure the phone has a stable WiFi connection and re-run `install.sh`.

**"`pkg` command not found" inside Ubuntu**
`pkg` is a Termux command, not a Linux/Ubuntu command. If you see this error, you are inside Ubuntu (you ran `proot-distro login ubuntu` earlier). Type `exit` to go back to Termux first, then use `pkg`.

**"`proot-distro` command not found"**
You are still inside Ubuntu. Type `exit` first to return to Termux.

**"start-autotrader.sh: No such file or directory"**
The install script could not copy the launcher to your Termux home. Use the fallback command:
```bash
proot-distro login ubuntu -- bash /root/autotrader/trading-system/scripts/android/bootstrap.sh
```

**"I don't know what my TOTP secret is"**
The TOTP secret is the key you originally scanned into your authenticator app when you set up 2FA on Groww. It looks like a random string of letters and numbers (e.g. `JBSWY3DPEHPK3PXP`). If you set 2FA up using Google Authenticator, try the app's built-in export/transfer feature — the secret may be visible there. Alternatively, log into Groww on a browser, go to Security settings, disable and re-enable 2FA, and when it shows the QR code look for a "can't scan? enter key manually" option — that text is your secret.

**The Telegram bot sends nothing, even after setup**
Before a bot can message you, you must send it at least one message first. Open Telegram, find your bot by its username (you chose this in @BotFather), and send it `/start`. After that, the bot can reach you.

**Dashboard opens but shows "WebSocket disconnected" or data doesn't update**
This usually means the backend started but WebSocket connections aren't reaching it. Check the `backend-log` tmux window (Ctrl-B 1) for errors. Also confirm you're accessing the dashboard from the same WiFi network as the phone.

**"How do I know if the system is actually trading?"**
Check the `backend-log` tmux window (Ctrl-B 1) — you'll see log lines for every scan, decision, and order. You'll also receive Telegram alerts for every trade entry, stop-loss hit, or target hit. If PAPER_TRADING=true in your .env, no real orders are placed — you'll still see all the same log activity but with `[PAPER]` noted.

**The phone's screen is off and I can't see anything**
That's fine — tmux keeps everything running with the screen off. Unlock the phone, open Termux, and run `tmux attach -t autotrader` (or `bash ~/start-autotrader.sh`) to re-attach. Do not swipe Termux out of Recents — that will kill it.

