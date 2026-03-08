# AutoTrader

An automated NSE intraday equity trading system. Streams live tick data from Zerodha Kite Connect, generates technical signals, and routes them through a Claude LLM decision engine before placing real or paper trades. Fully containerised — one command deploys the entire stack on a free Oracle Cloud VM.

---

## Architecture Overview

```
                     ┌─────────────────────────────────────────────────────┐
                     │                 FastAPI Backend                     │
09:15–15:30 IST      │                                                     │
                     │  ┌────────────┐    ┌─────────────────────────────┐  │
  Kite WebSocket ───►│  │  Scanner   │───►│     Decision Engine         │  │
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
  RSS / Google   ───►│  │  India VIX, financial news headlines          │   │
  News RSS feeds ───►│  │  → Claude synthesises → Market Brief         │   │
  NSE data       ───►│  │  → published to Redis + PostgreSQL           │   │
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
- Real-time OHLCV candle building from Kite WebSocket ticks (1-min and 5-min)
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
- Stop-loss and target monitoring with GTT orders on Kite
- Trailing stop-loss activation above configurable profit threshold
- Daily drawdown limit — halts all trading if breached
- Hard square-off of all positions at 15:00 IST
- Consecutive loss pause (configurable N losses → pause M minutes)
- Stock lock after stop-loss hit for remainder of the day

**Research Agent** (pre-market, 06:00–09:10 IST)
- Fetches SGX Nifty proxy (Nifty 50 close via Yahoo Finance `^NSEI`), DXY trend, US market close (Alpha Vantage)
- India VIX (NSE implied-volatility index via Yahoo Finance `^INDIAVIX`) — drives `recommended_stance` and `position_size_override`
- FII/DII net buy/sell data (NSE)
- Financial news headlines via `HybridNewsAggregator`: 5 Indian RSS feeds (Economic Times, Business Standard, Moneycontrol, LiveMint, NDTV Profit) + targeted Google News RSS per watchlist stock
- Claude synthesises all inputs into a structured `MarketBrief` with a `BULLISH / NEUTRAL / BEARISH` bias
- Bias is injected into every trade decision — a bearish brief suppresses long signals
- VIX regime gates position sizing: ELEVATED (20–25) → half-size; STRESS (>25) → avoid trading

**Trading Agent**
- Orchestrates Scanner → Decision Engine → Risk Manager lifecycle
- State recovery on restart: reconstructs open positions from Kite API
- Paper trading mode: full end-to-end simulation with zero real orders
- Telegram alerts for every notable event

**API & Dashboard**
- FastAPI REST + WebSocket backend
- Angular 17 frontend: live P&L, open positions, trade log, system alerts
- OAuth flow for daily Kite token refresh

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
| Broker | Zerodha Kite Connect v5 |
| LLM | Anthropic Claude — Sonnet 4.6 (Research Agent), Haiku 4.5 (Decision Engine) |
| Market data | Alpha Vantage (US close, DXY), Yahoo Finance (Nifty 50 close, India VIX), NSE HTTP |
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
│   │   └── token_refresh.py        # Daily Kite OAuth token renewal
│   ├── api/routes/                 # REST endpoints (trades, P&L, system, auth)
│   ├── api/websocket.py            # Live data WebSocket
│   ├── core/
│   │   ├── config.py               # Pydantic-settings config (all from .env)
│   │   ├── database.py             # Async SQLAlchemy engine
│   │   ├── redis_client.py
│   │   └── scheduler.py
│   ├── integrations/
│   │   ├── kite_client.py          # Kite Connect wrapper (retry + circuit breaker)
│   │   ├── anthropic_client.py
│   │   ├── alpha_vantage_client.py # US market close, DXY; Yahoo Finance Nifty 50 close + India VIX
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
├── deploy.sh                       # Oracle Cloud bootstrap script
├── PRODUCTION_SETUP.md             # Step-by-step production setup guide
└── PREPRODUCTION_CHECKLIST.md
```

---

## Quick Start (Local Development)

**Prerequisites:** Docker Desktop, Git

```bash
git clone https://github.com/YOUR_USERNAME/AutoTrader.git
cd AutoTrader/trading-system

# Create and fill in your .env
cp .env.example .env
# Edit .env: add KITE_API_KEY, ANTHROPIC_API_KEY, etc.

# Start the full stack
docker compose up --build

# In a separate terminal — apply migrations
docker compose exec backend alembic upgrade head
```

Open **http://localhost:4200** for the dashboard.
Open **http://localhost:8000/docs** for the API explorer.

For development without Kite credentials, leave `KITE_API_KEY` blank — the system
falls back to the mock tick generator (`MockTickGenerator`) automatically.

---

## Running Tests

```bash
cd trading-system/backend
python -m pytest tests/ -p no:warnings -q
# 151 passed, 2 failed (pre-existing test_api.py async fixture issues — unrelated to trading logic)
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
| `KITE_API_KEY` | Zerodha Kite Connect API key | — |
| `ALPHA_VANTAGE_API_KEY` | Alpha Vantage API key (US market close, DXY) | — |
| `ANTHROPIC_API_KEY` | Claude API key | — |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | — |
| `TELEGRAM_CHAT_ID` | Your personal Telegram chat ID | — |

See `trading-system/PRODUCTION_SETUP.md` for the full production deployment guide.

---

## Production Deployment

See **[PRODUCTION_SETUP.md](trading-system/PRODUCTION_SETUP.md)** for the complete step-by-step guide covering:

- Account setup (Zerodha, Anthropic, Oracle Cloud, Telegram)
- `.env` configuration
- Oracle Cloud VCN firewall rules
- `./deploy.sh` — one-command deploy on a fresh Ubuntu VM
- Daily Kite OAuth login flow
- Paper trading period guidance
- Going live checklist

**Cost summary:** Oracle VM is free. Kite Connect ₹2,000/month. Anthropic ~$20–40/month. Everything else free.

---

## Important Notes

- **Daily Kite login required.** Zerodha access tokens expire every day. The system sends a Telegram alert each morning — click the link, log in via browser, done. This is a Zerodha platform constraint with no workaround.
- **Long-only, NSE MIS (intraday).** The system does not hold overnight positions. All trades are squared off by 15:00 IST at the latest.
- **Not a backtesting framework.** The system is built for live event-driven trading. For historical backtesting, use a separate tool (vectorbt, backtrader) and replay Kite historical OHLCV data through the same signal conditions.
- **Start with `PAPER_TRADING=true`.** Run for at least 2 full trading weeks before enabling real orders.

---

## License

Private — all rights reserved.
