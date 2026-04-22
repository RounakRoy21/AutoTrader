# AutoTrader

An automated NSE intraday equity trading system. Streams live tick data from Groww API (GrowwFeed WebSocket), generates technical signals, and routes them through a Claude LLM decision engine before placing real or paper trades. Fully containerised — one command deploys the entire stack on a free Oracle Cloud VM.

---

## Architecture Overview

```
                     ┌─────────────────────────────────────────────────────┐
                     │                 FastAPI Backend                     │
09:15–15:30 IST      │                                                     │
                     │  ┌────────────┐    ┌─────────────────────────────┐  │
  GrowwFeed WS ───►│  │  Scanner   │───►│     Decision Engine         │  │
  (live ticks)       │  │            │    │  (Claude Haiku 4.5)         │  │
                     │  │  VWAP      │    │  SignalAudit thresholds:    │  │
                     │  │  RSI 14    │    │  • RSI 40–72                │  │
                     │  │  Volume ×  │    │  • Volume ≥ 1.5×            │  │
                     │  │  EMA/MACD  │    │  • VWAP dev 0–1.5%          │  │
                     │  │  ATR       │    │  • R:R ≥ 2.0                │  │
                     │  └────────────┘    └────────────┬────────────────┘  │
                     │                                 │                   │
                     │                    ┌────────────▼────────────────┐  │
                     │                    │      Trading Agent          │  │
                     │                    │  Order placement + GTT SL   │  │
                     │                    │  State recovery on restart  │  │
                     │                    └────────────┬────────────────┘  │
                     │                                 │                   │
                     │  ┌──────────────────────────────▼───────────────┐   │
                     │  │                 Risk Manager                 │   │
                     │  │  Poll every 5s — no LLM involved             │   │
                     │  │  Stop-loss / target monitoring               │   │
                     │  │  Trailing SL, daily drawdown halt            │   │
                     │  │  Hard square-off at 15:00 IST                │   │
                     │  └──────────────────────────────────────────────┘   │
                     │                                                     │
06:00–09:10 IST      │  ┌──────────────────────────────────────────────┐   │
                     │  │            Research Agent                    │   │
  Alpha Vantage  ───►│  │  SGX Nifty, DXY, US market close, FII/DII    │   │
  Yahoo Finance  ───►│  │  India VIX, crude oil (Brent), gold          │   │
  RSS / Google   ───►│  │  Earnings calendar (next 7 days)             │   │
  News RSS feeds ───►│  │  Financial news headlines                    │   │
  NSE data       ───►│  │  → Claude synthesises → Market Brief         │   │
                     │  │  → published to Redis + PostgreSQL           │   │
                     │  └──────────────────────────────────────────────┘   │
                     │                           │                         │
                     │              ┌────────────▼──────────┐              │
                     │              │  PostgreSQL           │              │
                     │              │  Redis (pub/sub, LTP) │              │
                     │              └───────────────────────┘              │
                     └─────────────────────────────────────────────────────┘
                                          │ REST + WebSocket
                                          ▼
                             ┌────────────────────────┐
                             │  Angular Dashboard     │
                             │  Live P&L              │
                             │  Open Positions        │
                             │  Trade Log             │
                             │  System Alerts         │
                             └────────────────────────┘
```

---

## Features

**Signal Generation (Scanner)**
- Real-time OHLCV candle building from GrowwFeed WebSocket ticks (1-min and 5-min)
- VWAP, RSI(14), Volume Ratio computed on every candle close
- EMA-9/21 alignment and MACD histogram for trend confirmation
- ATR-based SL and target sizing

**LLM Decision Engine**
- Claude evaluates every signal with a structured prompt and quantified thresholds
- `SignalAudit` schema: all factual fields (RSI, volume, VWAP) are overwritten from ground truth — the LLM cannot misreport them
- 3-layer validation pipeline independent of the LLM:
  - **Hard REJECT** — RSI extremes, low volume, price far above/below VWAP, R:R < 2, low confidence
  - **Soft REDUCE** — EMA misaligned, MACD negative, elevated VWAP deviation, any price below VWAP
  - **Conditions rebuild** — `conditions_not_met` list is derived from actual validation state, not LLM output
- VWAP=0 guard (scanner not yet warmed up at market open)

**Risk Manager** (zero LLM, deterministic)
- Poll-based (every 5 seconds) monitoring of all open positions
- Stop-loss and target monitoring with OCO orders on Groww
- Trailing stop-loss activation above configurable profit threshold
- Daily drawdown limit — halts all trading if breached
- Hard square-off of all positions at 15:00 IST
- Consecutive loss pause (configurable N losses → pause M minutes)
- Stock lock after stop-loss hit for remainder of the day

**Research Agent** (pre-market, 06:00–09:10 IST)
- Fetches SGX Nifty proxy (Nifty 50 close via Yahoo Finance `^NSEI`), DXY trend, US market close (Alpha Vantage)
- India VIX (NSE implied-volatility index via Yahoo Finance `^INDIAVIX`) — drives `recommended_stance` and `position_size_override`
- Brent crude oil (Yahoo Finance `CL=F`) and gold spot (Yahoo Finance `GC=F`) — feed commodity interpretation rules in the LLM prompt
- FII/DII net buy/sell data (NSE)
- Earnings calendar: upcoming NSE results in the next 7 days (Yahoo Finance `quoteSummary`) — populates `earnings_drift_candidates`; stocks with results today/tomorrow are flagged as high-uncertainty and bias the watchlist
- Financial news headlines via `HybridNewsAggregator`: 5 Indian RSS feeds (Economic Times, Business Standard, Moneycontrol, LiveMint, NDTV Profit) + targeted Google News RSS per watchlist stock
- Claude synthesises all inputs into a structured `MarketBrief` with a `BULLISH / NEUTRAL / BEARISH` bias
- Bias is injected into every trade decision — a bearish brief suppresses long signals
- VIX regime gates position sizing: ELEVATED (20–25) → half-size; STRESS (>25) → avoid trading

**Trading Agent**
- Orchestrates Scanner → Decision Engine → Risk Manager lifecycle
- State recovery on restart: reconstructs open positions from Groww API
- Paper trading mode: full end-to-end simulation with zero real orders
- Telegram alerts for every notable event

**API & Dashboard**
- FastAPI REST + WebSocket backend
- Angular 17 frontend: live P&L, open positions, trade log, system alerts
- TOTP-based Groww authentication (no daily re-login needed)

**Infrastructure**
- Docker Compose: PostgreSQL 16 + Redis 7 + FastAPI + nginx/Angular
- Alembic migrations
- Two-stage Docker build (non-root runtime user)
- Production compose overlay: no hot-reload, no exposed internal ports, log rotation
- Oracle Cloud Free Tier deploy script (`deploy.sh`)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.14 |
| API framework | FastAPI + uvicorn |
| Database | PostgreSQL 16, SQLAlchemy 2 async, asyncpg |
| Cache / pub-sub | Redis 7 |
| Migrations | Alembic |
| Scheduling | APScheduler 3.10 |
| Broker | Groww API (growwapi SDK) |
| LLM | Anthropic Claude — Sonnet 4.6 (Research Agent), Haiku 4.5 (Decision Engine) |
| Market data | Alpha Vantage (US close, DXY), Yahoo Finance (Nifty 50 close, India VIX, crude oil, gold, earnings calendar), NSE HTTP |
| News | HybridNewsAggregator — 5 Indian financial RSS feeds + Google News RSS (no API key) |
| Alerts | Telegram Bot API |
| Frontend | Angular 17, TypeScript |
| Containers | Docker, Docker Compose |
| Server | Oracle Cloud Free Tier (Ampere A1, Ubuntu) |

---

## Project Structure

```
trading-system/
├── backend/
│   ├── agents/
│   │   ├── scanner.py              # Tick ingestion, OHLCV candles, signal generation
│   │   ├── decision_engine.py      # LLM + SignalAudit validation pipeline
│   │   ├── trading_agent.py        # Order placement, state recovery
│   │   ├── trading_agent_manager.py
│   │   ├── risk_manager.py         # Deterministic position monitoring
│   │   ├── research_agent.py       # Pre-market data + market brief
│   │   └── token_refresh.py        # (stub) TOTP tokens do not expire
│   ├── api/routes/                 # REST endpoints (trades, P&L, system, auth)
│   ├── api/websocket.py            # Live data WebSocket
│   ├── core/
│   │   ├── config.py               # Pydantic-settings config (all from .env)
│   │   ├── database.py             # Async SQLAlchemy engine
│   │   ├── redis_client.py
│   │   └── scheduler.py
│   ├── integrations/
│   │   ├── groww_client.py         # Groww API wrapper (retry + circuit breaker)
│   │   ├── anthropic_client.py
│   │   ├── alpha_vantage_client.py # US close, DXY (Alpha Vantage); Nifty 50, India VIX,
│   │   │                           #   crude oil, gold, earnings calendar (Yahoo Finance)
│   │   ├── instrument_service.py   # NSE instrument list download + symbol resolution
│   │   ├── news_aggregator.py      # HybridNewsAggregator: RSS + Google News RSS
│   │   ├── nse_client.py           # FII/DII data
│   │   ├── telegram_client.py
│   │   ├── ltp_store.py            # In-memory LTP cache (Redis-backed)
│   │   └── mock_tick_generator.py  # ±2% random walk for offline dev
│   ├── models/                     # SQLAlchemy ORM models
│   ├── schemas/                    # Pydantic schemas (ScannerSignal, DecisionOutput, etc.)
│   ├── migrations/                 # Alembic migration scripts
│   ├── tests/                      # pytest test suite (150 tests)
│   ├── main.py                     # FastAPI app + lifespan startup
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   └── src/app/features/
│       ├── dashboard/
│       ├── open-positions/
│       ├── pnl-chart/
│       ├── system-alerts/
│       └── trade-log/
├── docker-compose.yml              # Dev stack
├── docker-compose.prod.yml         # Production overrides
├── deploy.sh                       # Oracle Cloud one-time bootstrap script
├── start.sh                        # Daily launch script (Linux / Oracle Cloud)
├── start.ps1                       # Daily launch script (Windows / Docker Desktop)
├── PRODUCTION_SETUP.md             # Step-by-step production setup guide
└── PREPRODUCTION_CHECKLIST.md
```

---

## Quick Start

### Prerequisites

| Requirement | Notes |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Windows / macOS — includes Compose |
| Docker Engine + Compose plugin | Linux — `curl -fsSL https://get.docker.com \| sh` |
| Git | For cloning |

---

### Step 1 — Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/AutoTrader.git
cd AutoTrader/trading-system

cp .env.example .env
```

Open `.env` and fill in the required values:

| Variable | Where to get it |
|---|---|
| `GROWW_CLIENT_ID` | [groww.in/trade-api/api-keys](https://groww.in/trade-api/api-keys) → Generate TOTP token |
| `GROWW_PASSWORD` | Not used in TOTP flow — leave blank |
| `GROWW_TOTP_SECRET` | Base32 secret shown alongside the TOTP token on the same page |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `TELEGRAM_BOT_TOKEN` | Create a bot via [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Message [@userinfobot](https://t.me/userinfobot) to find yours |
| `POSTGRES_PASSWORD` | Any strong random string |
| `DATABASE_URL` | Update the password portion to match `POSTGRES_PASSWORD` |
| `ADMIN_API_KEY` | Any strong random string (protects trading-control endpoints) |

> **Minimum viable `.env` for paper trading:** `GROWW_*`, `ANTHROPIC_API_KEY`, `POSTGRES_PASSWORD`, `DATABASE_URL`, `ADMIN_API_KEY`.
> `ALPHA_VANTAGE_API_KEY`, `TELEGRAM_*` are optional but recommended.

---

### Step 2 — Start the stack

**Windows (Docker Desktop):**
```powershell
.\start.ps1
```

**Linux / macOS:**
```bash
chmod +x start.sh && ./start.sh
```

The script will:
1. Verify `.env` is filled in and Docker is running
2. Start all 4 containers (postgres, redis, backend, frontend)
3. Wait for each service to pass its health check
4. Run Alembic database migrations automatically
5. Print URLs and status

Add `--build` (Linux) or `-Build` (Windows) to rebuild images after code changes.

---

### Step 3 — Authenticate with Groww (one-time only)

Groww TOTP tokens don’t expire, so this is done once:

```bash
curl -X POST http://localhost:8000/api/auth/groww/login \
  -H "Content-Type: application/json" \
  -d '{"client_id":"", "password":""}'
```

> Leave `client_id` and `password` empty — they are read from `.env` automatically.
> The TOTP code is auto-generated from `GROWW_TOTP_SECRET`.

Verify: `GET http://localhost:8000/api/auth/groww/status` should return `{"authenticated": true}`.

---

### Step 4 — Open the dashboard

| URL | What it is |
|---|---|
| http://localhost:4200 | Angular dashboard (live P&L, positions, alerts) |
| http://localhost:8000/docs | FastAPI interactive API explorer |
| http://localhost:8000/api/health | Health check (broker status, agent status) |

---

### Stopping and restarting

```bash
./start.sh --stop        # graceful shutdown (preserves database volume)
./start.sh               # restart
./start.sh --status      # show container status
```

Windows:
```powershell
.\start.ps1 -Stop
.\start.ps1 -Status
```

---

### Offline / no-credentials development

Leave `GROWW_CLIENT_ID` blank in `.env`. The system automatically falls back to `MockTickGenerator`, which simulates realistic ±2% random-walk tick data. All signal logic, the decision engine, risk manager, and database writes function identically.

---

## Running Tests

```bash
cd trading-system/backend
python -m pytest tests/ -p no:warnings -q
# 153 passed
```

---

## Configuration Reference

All configuration lives in `trading-system/.env`. Key variables:

| Variable | Description | Default |
|---|---|---|
| `PAPER_TRADING` | `true` = simulate orders, no real trades | `true` |
| `TOTAL_CAPITAL` | Capital allocated to the system (₹) | `1000000.0` |
| `MAX_OPEN_POSITIONS` | Maximum simultaneous open positions | `3` |
| `MAX_TRADES_PER_DAY` | Hard daily trade count ceiling | `6` |
| `DAILY_DRAWDOWN_LIMIT_PCT` | % loss that triggers a trading halt | `0.03` |
| `GROWW_CLIENT_ID` | Groww account client ID | — |
| `GROWW_PASSWORD` | Groww account password | — |
| `GROWW_TOTP_SECRET` | Base32 TOTP secret for 2FA | — |
| `ALPHA_VANTAGE_API_KEY` | Alpha Vantage API key (US market close, DXY) | — |
| `ANTHROPIC_API_KEY` | Claude API key | — |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | — |
| `TELEGRAM_CHAT_ID` | Your personal Telegram chat ID | — |

See `trading-system/PRODUCTION_SETUP.md` for the full production deployment guide.

---

## Production Deployment

See **[PRODUCTION_SETUP.md](trading-system/PRODUCTION_SETUP.md)** for the complete step-by-step guide covering:

- Account setup (Groww, Anthropic, Oracle Cloud, Telegram)
- `.env` configuration
- Oracle Cloud VCN firewall rules
- `./deploy.sh` — one-command deploy on a fresh Ubuntu VM
- One-time Groww TOTP authentication via POST /api/auth/groww/login
- Paper trading period guidance
- Going live checklist

**Cost summary:** Oracle VM is free. Groww API access free. Anthropic ~$20–40/month. Everything else free.

---

## Important Notes

- **No daily re-authentication.** Groww TOTP tokens do not expire. Authenticate once with `POST /api/auth/groww/login` and the token persists in Redis until you explicitly log out.
- **Long-only, NSE MIS (intraday).** The system does not hold overnight positions. All trades are squared off by 15:00 IST at the latest.
- **Not a backtesting framework.** The system is built for live event-driven trading. For historical backtesting, use a separate tool (vectorbt, backtrader) and replay Groww historical OHLCV data through the same signal conditions.
- **Start with `PAPER_TRADING=true`.** Run for at least 2 full trading weeks before enabling real orders.

---

## License

Private — all rights reserved.
