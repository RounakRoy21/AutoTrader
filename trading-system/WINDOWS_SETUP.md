# AutoTrader — Windows PC Setup Guide

A to-the-point guide to get AutoTrader running on any Windows machine and keep it running throughout the trading week.

---

## Prerequisites

Install these once, in order:

1. **Git** — https://git-scm.com/download/win
2. **Docker Desktop** — https://www.docker.com/products/docker-desktop
   - During install: enable "Start Docker Desktop when you log in"
   - After install: open Docker Desktop, go to **Settings → General**, confirm "Start Docker Desktop when you log in" is ticked

---

## Step 1 — Get the Code

Open PowerShell and run:

```powershell
cd C:\                          # or wherever you want to put it
git clone <your-repo-url> AutoTrader
cd AutoTrader\trading-system
```

---

## Step 2 — Create the `.env` File

Copy the example and fill in your credentials:

```powershell
copy .env.example .env          # if one exists, otherwise create it fresh
notepad .env
```

Minimum required entries:

```env
# --- Groww API ---
GROWW_CLIENT_ID=your_groww_client_id
GROWW_PASSWORD=your_groww_password
GROWW_TOTP_SECRET=your_base32_totp_secret   # from your authenticator app setup

# --- Anthropic (Claude) ---
ANTHROPIC_API_KEY=sk-ant-...

# --- Telegram (for trade alerts) ---
TELEGRAM_BOT_TOKEN=123456:ABC-...
TELEGRAM_CHAT_ID=your_chat_id

# --- Trading mode ---
PAPER_TRADING=true              # set to false only when ready for live money

# --- Security (set a random string when exposing on a network) ---
ADMIN_API_KEY=change_me_to_something_random

# --- Anthropic Admin API (token usage reporting) ---
# Uses the same key as ANTHROPIC_API_KEY above — no separate credential needed.
```

Optional but recommended for live trading:

```env
ALPHA_VANTAGE_API_KEY=your_key  # for news sentiment in market briefs
```

Leave everything else at its default. PostgreSQL and Redis are fully self-contained inside Docker — no external database setup needed.

---

## Step 3 — First-Time Build and Start

```powershell
.\start.ps1 -Build
```

This builds all Docker images and starts four containers: `postgres`, `redis`, `backend`, `frontend`. Takes 3–5 minutes the first time. On subsequent starts it's under 30 seconds.

When complete, open your browser:

| URL | What it is |
|-----|------------|
| `http://localhost:4201` | Trading dashboard |
| `http://localhost:8000/docs` | Backend API docs |

---

## Step 4 — Daily Operations

| Task | Command |
|------|---------|
| Start everything | `.\start.ps1` |
| Start + rebuild images after a code change | `.\start.ps1 -Build` |
| Stop gracefully | `.\start.ps1 -Stop` |
| Check container status | `.\start.ps1 -Status` |

The database volume is **preserved** across stop/start cycles. Your trade history is never lost by stopping the app.

---

## Step 5 — Keep It Running All Week (Spare PC)

Do these once to make the laptop self-managing:

### Disable sleep
Settings → System → Power & Sleep → set both dropdowns to **Never** (plugged in).

### Auto-login after reboot
Press `Win + R`, type `netplwiz`, untick "Users must enter a username and password". This lets Docker start automatically after a power cut or forced reboot without needing someone at the keyboard.

### Auto-start the app on login
Open **Task Scheduler** → Create Basic Task:
- **Trigger**: At log on
- **Action**: Start a program
  - Program: `powershell.exe`
  - Arguments: `-NonInteractive -File "C:\AutoTrader\trading-system\start.ps1"`
- **Conditions**: untick "Start only if the computer is on AC power"

### Disable automatic restart from Windows Update
Settings → Windows Update → Advanced Options → set "Notify to schedule restart" (not automatic). Apply updates manually on Saturday when the system is off.

---

## Step 6 — Access from Another Computer (Optional)

Install **Tailscale** (free, https://tailscale.com) on both machines. Once connected, access the dashboard from your main laptop at:

```
http://<spare-laptop-tailscale-ip>:4201
```

No port forwarding, no firewall rules needed. Telegram alerts will still reach you regardless.

---

## Weekly Routine

| When | Action |
|------|--------|
| **Monday morning** | Power on the laptop. If Task Scheduler is configured, nothing else to do — app starts automatically. Verify on the dashboard. |
| **Mon–Fri** | Monitor via Telegram alerts and the dashboard. The market-hours gate prevents any activity before 09:15 and after 15:30 IST automatically. |
| **Saturday** | Run `.\start.ps1 -Stop`, then shut down the laptop. |
| **After any unexpected reboot** | Containers restart themselves via Docker's `restart: unless-stopped` policy. No manual action needed. |

---

## Troubleshooting

**Docker Desktop not starting?**
Right-click the Docker icon in the system tray → Restart. Or reboot the PC.

**`start.ps1` says "docker not found"?**
Log out and log back in after installing Docker Desktop. The PATH update requires a fresh session.

**Backend container keeps restarting?**
Check the logs: `docker-compose logs backend --tail=50`. Usually a missing or wrong value in `.env`.

**Dashboard shows "Disconnected"?**
The WebSocket connects to the backend. Refresh the page once the backend is fully up (~10 seconds after `docker-compose up`).

**Forgot to stop before shutdown?**
Docker handles this gracefully. The containers will resume on next start via `restart: unless-stopped`.
