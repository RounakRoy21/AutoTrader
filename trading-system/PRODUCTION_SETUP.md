# AutoTrader — Production Setup Guide

> **One-time reference** for getting the full stack running on Oracle Cloud with real data.
> Read the "Is it ready?" section first — it sets honest expectations.

---

## Is It Ready?

### ✅ Ready for: paper trading + live trading

The system is **production-complete** for real intraday trading (NSE MIS, long-only).
All eight build phases are done:

| What works | Detail |
|---|---|
| Live tick ingestion | Kite WebSocket → CandleBuilder → VWAP/RSI/Volume signals |
| LLM decision making | Claude evaluates every signal with quantified thresholds |
| Signal audit & guard | 3-layer validator (hard reject → soft reduce → conditions audit) |
| Risk management | Stop-loss, GTT, trailing SL, daily drawdown halt, square-off |
| Trade persistence | PostgreSQL via SQLAlchemy async |
| Dashboard | Angular frontend with live P&L, WebSocket feed |
| Docker deployment | Single command brings up database, cache, backend, frontend |
| Alerts | Telegram notifications for every trade event |

### ❌ Not ready for: historical backtesting

The system is built as a **live event-driven system**, not a backtesting framework. It streams
real-time ticks from Kite WebSocket and makes decisions in OHLCV candle-time. To backtest,
you would need to feed historical OHLCV data through a replay harness — that component does not
exist yet.

**Practical alternative:** run the system in `PAPER_TRADING=true` mode during live market hours
for 2–4 weeks. That IS the functional equivalent of forward-testing before going live with real
capital, and it is what the system is designed for.

If you need true historical backtesting, the right tool is a separate framework
(e.g. [vectorbt](https://vectorbt.pro/) or [backtrader](https://www.backtrader.com/)) where you
re-implement the same signal conditions (RSI 40–72, volume 1.5×, VWAP 0–1.5%) and run them over
Kite historical data or NSE EOD dumps. That is a separate project.

---

## Dependency Map

```
┌─────────────────────────────────────────────────────────────┐
│  Oracle Cloud VM (Ubuntu, Docker)                           │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────────────┐  │
│  │ Postgres │  │  Redis   │  │  FastAPI backend          │  │
│  │  :5432   │  │  :6379   │  │  :8000                    │  │
│  └──────────┘  └──────────┘  │  ├─ Kite Connect SDK      │  │
│                              │  ├─ Anthropic SDK         │  │
│  ┌──────────────────────────┐│  ├─ Alpha Vantage HTTP    │  │
│  │  nginx + Angular  :80    ││  ├─ Yahoo Finance HTTP    │  │
│  └──────────────────────────┘│  ├─ Indian RSS feeds      │  │
│                              │  └─ Telegram Bot          │  │
│                              └───────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
           │               │              │
  Zerodha Kite        Anthropic       Alpha Vantage / Yahoo Finance
  (broker + ticks)   (Claude LLM)    (market data)
```

---

## Step 1 — Create Accounts

Do all of these before touching any config files.

### 1.1 Zerodha (mandatory — this is the broker)

1. Open a Zerodha trading + demat account at **zerodha.com**.
   Takes 1–2 days for verification if you have Aadhaar.
2. Log in to the **Kite Connect developer portal**: [kite.trade/developers](https://kite.trade/developers)
3. Click **Create new app**.
   - App name: anything (e.g. `AutoTrader`)
   - App type: **Connect**
   - Redirect URL: `http://<YOUR_SERVER_IP>/api/auth/kite/callback`
     (use `http://localhost:8000/api/auth/kite/callback` for local dev)
4. Subscribe to Kite Connect: **₹2,000/month** (billed to Zerodha) — this is unavoidable.
5. Note down `API Key` and `API Secret` from the app page.

> **Important — daily login requirement:** Kite Connect tokens expire every day at ~07:30 IST.
> The system has a Telegram alert that fires each morning to remind you. You click a link,
> log in via browser, and the new token is stored in Redis automatically.
> There is **no fully unattended overnight restart** — this is a Zerodha architectural constraint.

### 1.2 Anthropic (mandatory — powers the Decision Engine)

1. Sign up at **console.anthropic.com**.
2. Go to **API Keys** → **Create Key**.
3. Note down the key (shown once).
4. Add billing — Claude Sonnet typically costs ~$3–5/day at moderate trading volumes.

### 1.3 Alpha Vantage (optional but recommended — used by Research Agent)

1. Sign up free at **alphavantage.co/support/#api-key**.
2. Free tier: 25 requests/day — sufficient for end-of-day market context (S&P 500, NASDAQ, DXY).
3. Note down the API key.

> **Note:** India VIX and Nifty 50 directional proxy are fetched from **Yahoo Finance** (`^INDIAVIX`, `^NSEI`) — free, no registration, no key required.
> Indian financial news is gathered from **5 RSS feeds** (Economic Times, Business Standard, Moneycontrol, LiveMint, NDTV Profit) plus Google News RSS — also free, no key.
> A `NEWSAPI_API_KEY` is no longer used.

### 1.4 Telegram (strongly recommended — all trade alerts go here)

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` → give it a name (e.g. `AutoTrader Bot`).
3. BotFather replies with your **bot token** (looks like `123456789:ABCDef...`).
4. To get your personal **chat ID**:
   - Search for **@userinfobot** in Telegram and send it any message.
   - It replies with your chat ID (a number like `987654321`).
   - Alternatively — start a chat with your new bot, then open:
     `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
     Your chat ID is in the JSON under `message.chat.id`.

### 1.5 Oracle Cloud Free Tier (the server)

1. Sign up at **cloud.oracle.com** — requires a credit card for identity, but
   the Ampere A1 tier is **always free** (4 OCPUs, 24 GB RAM, 200 GB storage).
2. Log in → **Compute** → **Instances** → **Create Instance**.
3. Settings:
   - **Image:** Ubuntu 22.04 (Minimal)
   - **Shape:** `VM.Standard.A1.Flex` → set **4 OCPUs / 24 GB RAM**
     (this is within the free tier limit)
   - **Boot volume:** 100 GB
   - **SSH keys:** upload your public key (generate one below if needed)
4. Instance boots in about 2 minutes. Note the **Public IP address**.

**Generate SSH key (Windows PowerShell):**
```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\autotrader_oracle"
# Copy contents of autotrader_oracle.pub when prompted for public key
```

**Open required ports in OCI Console (mandatory — Docker alone is not enough):**
1. In OCI: **Networking** → **Virtual Cloud Networks** → your VCN
2. **Security Lists** → **Default Security List** → **Add Ingress Rules**
3. Add these rules:

   | Protocol | Source CIDR | Port | Description |
   |---|---|---|---|
   | TCP | 0.0.0.0/0 | 80 | Dashboard (nginx) |
   | TCP | 0.0.0.0/0 | 22 | SSH |

   You do not need to expose 8000 or 5432 publicly — they stay internal to Docker.

---

## Step 2 — Prepare the Server

SSH into your Oracle instance:

```bash
ssh -i ~/.ssh/autotrader_oracle ubuntu@<YOUR_SERVER_IP>
```

Clone the repo:

```bash
git clone https://github.com/YOUR_USERNAME/AutoTrader.git
cd AutoTrader/trading-system
```

**Or** copy the project from your Windows machine:

```powershell
# Run from Windows PowerShell
scp -i $env:USERPROFILE\.ssh\autotrader_oracle -r `
  "C:\repos\Personal\Projects\AutoTrader\trading-system" `
  ubuntu@<YOUR_SERVER_IP>:~/autotrader
```

---

## Step 3 — Configure Environment Variables

On the server, inside the `trading-system/` folder:

```bash
cp .env.example .env
nano .env        # or: vim .env
```

Fill in every value — this is the only place secrets live:

```dotenv
# ── Application ────────────────────────────────────────
APP_ENV=production
LOG_LEVEL=INFO
PAPER_TRADING=true          # ← keep true until you've forward-tested 2+ weeks

# ── PostgreSQL ─────────────────────────────────────────
POSTGRES_HOST=postgres      # Docker service name — do not change
POSTGRES_PORT=5432
POSTGRES_DB=autotrader
POSTGRES_USER=autotrader
POSTGRES_PASSWORD=<strong-random-password>     # e.g. openssl rand -hex 24
DATABASE_URL=postgresql+asyncpg://autotrader:<PASSWORD>@postgres:5432/autotrader
DATABASE_URL_SYNC=postgresql://autotrader:<PASSWORD>@postgres:5432/autotrader

# ── Redis ───────────────────────────────────────────────
REDIS_HOST=redis            # Docker service name — do not change
REDIS_PORT=6379
REDIS_PASSWORD=             # leave blank is fine on a private Docker network
REDIS_URL=redis://redis:6379/0

# ── Zerodha ─────────────────────────────────────────────
KITE_API_KEY=<your API Key from kite.trade/developers>
KITE_API_SECRET=<your API Secret>
KITE_REDIRECT_URL=http://<YOUR_SERVER_IP>/api/auth/kite/callback

# ── Anthropic ───────────────────────────────────────────
ANTHROPIC_API_KEY=<sk-ant-api03-...>
ANTHROPIC_MODEL=claude-sonnet-4-20250514

# ── Alpha Vantage ───────────────────────────────────────
ALPHA_VANTAGE_API_KEY=<your key>
# India VIX and Nifty 50 directional proxy are fetched from Yahoo Finance (^INDIAVIX, ^NSEI)
# (free, no key needed)
# News is fetched from Indian RSS feeds + Google News RSS (free, no key needed)

# ── Telegram ────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=<123456789:ABCDef...>
TELEGRAM_CHAT_ID=<987654321>

# ── Capital & Risk ──────────────────────────────────────
TOTAL_CAPITAL=100000.0          # ₹1 lakh is a safe starting amount
MAX_OPEN_POSITIONS=3
MAX_TRADES_PER_DAY=6
DAILY_DRAWDOWN_LIMIT_PCT=0.03
```

**Generate a strong Postgres password:**
```bash
openssl rand -hex 24
```

---

## Step 4 — Deploy

The `deploy.sh` script handles Docker installation, firewall rules, and first startup.

```bash
cd ~/autotrader/trading-system    # wherever you cloned/copied it
chmod +x deploy.sh
./deploy.sh
```

What it does:
- Installs Docker + Docker Compose if not present
- Opens ports 80 and 22 via `iptables`
- Detects your `.env` file
- Builds and starts all containers: `postgres`, `redis`, `backend`, `frontend`

After the build (takes 3–5 minutes the first time), verify everything is up:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

All four containers should show `healthy` or `running`.

---

## Step 5 — Run Database Migrations

**Done once after first deploy, and after any future schema changes:**

```bash
docker compose exec backend alembic upgrade head
```

This creates all tables: `trades`, `market_briefs`, `daily_pnl`, `agent_logs`.

Verify:
```bash
docker compose exec postgres psql -U autotrader -d autotrader -c "\dt"
```

You should see 4–5 tables.

---

## Step 6 — Daily Kite Login (every trading day)

Kite Connect tokens expire overnight. Each morning before market open:

1. Your Telegram bot will send a message: *"⚠ Kite token expired — please log in"*
2. Open a browser and go to: `http://<YOUR_SERVER_IP>/api/auth/kite/login`
3. Redirects to Zerodha's login page — log in with your Zerodha credentials + TOTP.
4. Zerodha redirects back to your server's callback URL.
5. The backend stores the new token in Redis.
6. Telegram confirms: *"✅ Kite token refreshed"*

> Do this between **08:45–09:00 IST** — before `check_kite_token` fires at 09:00 IST
> and the Scanner starts subscribing to ticks at market open (09:15 IST).

---

## Step 7 — Verify the System is Live

Open the dashboard in a browser:

```
http://<YOUR_SERVER_IP>
```

Check each section:

| What you should see | Means |
|---|---|
| System status → **Database: OK** | Postgres connected |
| System status → **Redis: OK** | Redis connected |
| System status → **Kite: connected** | Today's token is valid |
| Market Brief section populates by 09:05 IST | Research Agent ran |
| Positions table starts updating after 09:15 | Scanner is running |
| Telegram message: *"Market open"* | Trading session started |

---

## Step 8 — Monitoring & Logs

**Live backend logs:**
```bash
docker compose logs -f backend
```

**Follow a specific agent:**
```bash
docker compose logs -f backend 2>&1 | grep "scanner\|decision\|trading_agent"
```

**Check health endpoint:**
```bash
curl http://<YOUR_SERVER_IP>/api/health
# → {"status":"ok","database":"ok","redis":"ok"}
```

**Database: view today's trades:**
```bash
docker compose exec postgres psql -U autotrader -d autotrader \
  -c "SELECT stock, direction, entry_price, status, pnl FROM trades WHERE trade_date = CURRENT_DATE;"
```

---

## Step 9 — Paper Trading Period (do not skip)

Keep `PAPER_TRADING=true` in `.env` for at least **2 full trading weeks** before any live capital.

During this period, the system will:
- Generate real signals from live Kite tick data
- Run those signals through Claude decision engine with real thresholds
- Simulate order placement (log only, no real Zerodha orders)
- Track paper P&L in the database
- Send all normal Telegram alerts

Watch for:
- Signals firing at sensible times (not pre-market noise)
- Decision Engine rejecting borderline signals (check logs for "Hard rule: …")
- Daily drawdown halt triggering when it should
- No runaway trade counts (should stay ≤ `MAX_TRADES_PER_DAY`)

---

## Step 10 — Going Live

Only do this after 2+ weeks of satisfactory paper trading results.

**Edit `.env` on the server:**
```bash
nano .env
# Change:  PAPER_TRADING=true  →  PAPER_TRADING=false
```

**Restart the backend:**
```bash
docker compose restart backend
```

**Confirm in logs:**
```bash
docker compose logs backend | grep "PAPER_TRADING\|paper"
# Should see: "Paper trading mode: DISABLED"
```

From this point, every `EXECUTE` decision places a real Zerodha market order.
Start with a **small capital** (₹50k–₹1 lakh). Scale up only after 2–4 weeks of
consistent live results.

---

## Useful Ongoing Commands

```bash
# Restart backend only (e.g. after .env change):
docker compose restart backend

# Pull latest code and redeploy:
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build backend

# Run migrations after a schema update:
docker compose exec backend alembic upgrade head

# Manually trigger the Research Agent (useful for testing):
curl -X POST http://localhost:8000/api/market-brief/run

# Stop everything (safe — postgres data is in a named volume):
docker compose down

# Full reset including database (⚠ destroys all trade history):
docker compose down -v
```

---

## Troubleshooting Quick Reference

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboard shows "Kite: disconnected" | Token expired or not yet set | Do Step 6 |
| Scanner not firing signals | Token issue or no stocks on watchlist | Check logs; verify Kite token |
| Decision Engine logs "placeholder key" | `ANTHROPIC_API_KEY` is blank in `.env` | Fill in the key, restart backend |
| Port 80 not reachable from browser | OCI VCN ingress rule missing | Add TCP 80 ingress in OCI console |
| `alembic upgrade head` fails | Database not yet reachable | Wait for postgres healthcheck, retry |
| Telegram alerts not arriving | Wrong `TELEGRAM_CHAT_ID` | Verify by sending `/start` to your bot |
| High Anthropic costs | Signals firing on every tick | Normal — each signal = 1 LLM call; reduce watchlist if needed |

---

## Cost Estimate (monthly)

| Service | Cost |
|---|---|
| Oracle Cloud VM (Ampere A1 4 OCPU / 24 GB) | **Free** |
| Zerodha Kite Connect subscription | **₹2,000/month** |
| Anthropic Claude Sonnet (~30 trades/day, 22 days) | **~$20–40/month** |
| Alpha Vantage (free tier) | **Free** |
| Yahoo Finance (^NSEI, ^INDIAVIX) | **Free** |
| Indian RSS feeds + Google News RSS | **Free** |
| Telegram Bot API | **Free** |
| **Total** | **~₹4,500–6,000/month** |

Brokerage charges (₹20/order at Zerodha) are separate and come out of trading capital.
