# AutoTrader — On-Device Android (Kotlin) Migration Plan

> **Status:** Research / architecture spike complete. No port code written yet.
> **Last updated:** 2026-06-25
> **Purpose of this document:** A self-contained record so that if the chat session is lost,
> we (or a future agent) can resume the Android port from exactly this point. It captures the
> objective, the chosen approach, the full Python→Kotlin component mapping, the Groww REST
> endpoint map, the migration gotchas, the runtime/scheduler design, the business logic that
> must be preserved bit-for-bit, and the phased roadmap with its riskiest unknowns.

---

## 1. Objective & Context

**Goal:** Run the entire AutoTrader system **on-device** as a single Kotlin Android APK on a
**OnePlus Nord** (OxygenOS), with **no cloud/server dependency**, so the automated NSE
intraday trading loop runs locally on the phone.

**Why this came up:** The original plan was to host the existing Python+Angular stack on Oracle
Cloud (OCI). Account creation was blocked by **Oracle's CyberSource fraud detection
(Reason Code 481, Rule CSDM000)** and could not be resolved. On-device Android became the
chosen alternative because the phone is always-on, always-connected, and free to run.

**Decision taken:** **Option A** — reimplement the trading "brain" natively in Kotlin, and
**reuse the existing Angular dashboard unchanged** by bundling its production build as APK
assets and loading it in a WebView, fed by an in-process local server / direct bridge. The user
explicitly wanted a **feasibility-first, phased** approach: prove the risky pieces before
committing to a full rewrite.

**Feasibility verdict:** Feasible. Every external dependency the backend uses is plain HTTPS/REST
(Groww, Anthropic, Alpha Vantage, NSE, Telegram, RSS news). The only Python-SDK-exclusive feature
— the GrowwFeed websocket — is **not required**, because Groww's documented Live-Data REST
endpoints provide the same data via polling, which the scanner already does as a supplement.

---

## 2. Source System Overview (what we are porting)

A fully automated NSE equity intraday/positional trading system.

**Backend (Python):** FastAPI + uvicorn, SQLAlchemy[asyncio] 2.x + asyncpg + PostgreSQL,
Redis (pub/sub + KV state), APScheduler 3.10 (AsyncIOScheduler, IST cron), growwapi SDK,
pyotp (TOTP), pandas/numpy (TA), anthropic (Claude), python-telegram-bot, feedparser, httpx/aiohttp.

**Frontend (Angular 17):** Material, chart.js/ng2-charts, rxjs, WebSocket client. Production build
served by nginx in the current Docker deployment.

**Runtime topology today:** 3 Docker containers — `autotrader-backend`, `autotrader-frontend`
(nginx), `autotrader-postgres` — plus Redis.

### Trading-day timeline (IST) — must be preserved exactly
| Time (IST) | Action | Source |
|---|---|---|
| 06:00 | Research Agent pre-market run → market brief | APScheduler cron |
| 08:30 | Daily Groww re-authentication | APScheduler cron |
| 09:15 | Trading session **start** (Scanner+Decision+Risk) | APScheduler cron |
| 09:15–15:30 | Live scanning, decisions, risk management | running loop |
| 12:30 | Research Agent mid-session refresh (guarded vs 45-min recent run) | APScheduler cron |
| 15:00 | MIS intraday square-off begins | Risk Manager (`MIS_CLOSE_START`) |
| 15:15 | Hard cutoff: no new entry signals | Scanner (`SIGNAL_CUTOFF`) |
| 15:30 | Trading session **stop** | APScheduler cron |
| EOD | EOD P&L report | Risk Manager (`EOD_REPORT_TIME`) |

Notes: Fridays stop generating signals after 14:00. NSE-holiday guard skips the whole day.
On a mid-day backend restart, both research and trading sessions **catch up** automatically.

---

## 3. Target Android Architecture (Option A)

```
┌──────────────────────────── Android APK (OnePlus Nord) ────────────────────────────┐
│                                                                                     │
│  Foreground Service "TradingService" (the brain — survives screen-off)              │
│  ├─ CoroutineScope(SupervisorJob + Dispatchers.Default)                             │
│  ├─ Scheduler (AlarmManager.setExactAndAllowWhileIdle + WorkManager)                │
│  │     06:00 research · 08:30 reauth · 09:15 start · 12:30 research · 15:30 stop    │
│  ├─ ResearchAgent  ─┐                                                               │
│  ├─ Scanner         │  (coroutines, replace asyncio tasks/threads)                  │
│  ├─ DecisionEngine  │                                                               │
│  ├─ RiskManager     ┘                                                               │
│  ├─ GrowwRestClient (OkHttp/Retrofit)  ── polls LTP/Quote, places/cancels orders    │
│  ├─ State: in-memory (StateFlow/SharedFlow) + DataStore (KV) + Room/SQLite (trades) │
│  └─ EventBus: MutableSharedFlow  (replaces Redis pub/sub channels)                  │
│                                                                                     │
│  Local HTTP server (Ktor embedded or NanoHTTPD) on 127.0.0.1:<port>                 │
│  ├─ Serves bundled Angular build (assets/www) → WebView                             │
│  ├─ REST routes mirroring FastAPI /api/* (read from Room/DataStore/StateFlow)       │
│  └─ WebSocket /ws/live  ← bridges EventBus + LTP snapshots to the dashboard         │
│                                                                                     │
│  WebView (MainActivity)  ── loads http://127.0.0.1:<port> → existing dashboard UI   │
└─────────────────────────────────────────────────────────────────────────────────────┘
        │HTTPS                 │HTTPS              │HTTPS            │HTTPS
   api.groww.in         api.anthropic.com   alphavantage/NSE   api.telegram.org
```

**Key principle:** keep the Angular app and its server contract (`/api/*` + `/ws/live`) identical,
so the frontend is reused with zero changes. The Kotlin local server reproduces just enough of the
FastAPI surface the dashboard consumes.

---

## 4. Python → Kotlin Component Mapping

| Python module / concern | Role today | Kotlin / Android equivalent |
|---|---|---|
| `main.py` lifespan | startup orchestration, catch-up, scheduling | `TradingService.onCreate()` + an `AppBootstrap` coroutine |
| `core/scheduler.py` (APScheduler, IST cron) | 06:00/08:30/09:15/12:30/15:30 jobs | `AlarmManager.setExactAndAllowWhileIdle` (exact wakeups) + `WorkManager` (resilient retries); compute next IST trigger with `java.time` + `ZoneId.of("Asia/Kolkata")` |
| `core/config.py` (pydantic-settings, `.env`) | typed config + derived capital helpers | Kotlin `data class Settings` loaded from `BuildConfig`/`DataStore`/encrypted prefs; keep all numeric thresholds **identical** |
| `core/database.py` (SQLAlchemy async + Postgres) | trade/brief/pnl/log persistence | **Room** (SQLite) DAOs + entities |
| `core/redis_client.py` (KV + pub/sub) | shared state + event bus | KV → **DataStore** (persistent) + in-memory map for ephemeral; pub/sub → **`MutableSharedFlow`** EventBus |
| `core/redis_keys.py` | central key registry | a `StateKeys` object + typed `StateStore` wrapper |
| `core/nse_calendar.py` | NSE holiday + IST today | port the holiday list to Kotlin; `LocalDate.now(istZone)` |
| `agents/trading_agent_manager.py` | session lifecycle, trade-count restore, halt reset | `TradingSessionManager` (singleton) coordinating coroutine `Job`s |
| `agents/trading_agent.py` | orchestrates Scanner/Decision/Risk, order placement, fill verify, GTT, DB persist, state recovery | `TradingOrchestrator` coroutine; same order→fill→SL→persist sequence |
| `agents/scanner.py` (pandas OHLCV, VWAP/RSI/EMA/MACD/ATR, signal conditions) | tick→indicator→signal | `Scanner` coroutine; **TA4J** or hand-rolled Kotlin indicators over a ring buffer; **poll-driven ticks** (see §7) |
| `agents/decision_engine.py` (pre-checks + Claude LLM + bias×stance matrices) | gate + size + confirm trades | `DecisionEngine`; pre-checks pure Kotlin; LLM via Anthropic REST (OkHttp) |
| `agents/risk_manager.py` (thread + 5s poll loop) | SL/target/trailing/ROI-decay/partial/EOD/drawdown | `RiskManager` coroutine with `delay(5_000)` loop; identical rule order |
| `agents/research_agent.py` (multi-API + Claude → brief) | pre-market brief + watchlist | `ResearchAgent` coroutine; same external calls via REST |
| `integrations/groww_client.py` (SDK wrapper + feed adapter) | broker I/O | **`GrowwRestClient`** (Retrofit) — see §5 & §6; **no websocket** |
| `integrations/instrument_service.py` (symbol↔token) | instrument map | download public CSV, parse (kotlin-csv), build map; keep `FALLBACK_TOKEN_MAP` |
| `integrations/anthropic_client.py` | Claude calls | OkHttp + Anthropic Messages REST |
| `integrations/alpha_vantage_client.py` | macro data | OkHttp REST |
| `integrations/nse_client.py` | FII/DII, indices, bulk deals | OkHttp REST (mind NSE cookie/User-Agent handling) |
| `integrations/news_aggregator.py` (feedparser) | RSS news | **Rome** or a Kotlin RSS/XML parser |
| `integrations/telegram_client.py` | alerts | OkHttp → Telegram Bot API |
| `integrations/ltp_store.py` (in-memory LTP) | latest prices | in-memory `ConcurrentHashMap` + `StateFlow` |
| `integrations/mock_tick_generator.py` | paper-mode synthetic ticks | Kotlin mock tick coroutine |
| `api/routes/*.py` (FastAPI) | dashboard REST | Ktor routes in the local server (only what the UI calls) |
| `api/websocket.py` (`/ws/live` relay + 2s LTP) | push to dashboard | Ktor WebSocket bridging the EventBus + 2s LTP snapshots |
| `models/*.py` (SQLAlchemy ORM) | Trade, MarketBrief, DailyPnl, AgentLog | Room `@Entity` + `@Dao` |
| `schemas/*.py` (pydantic) | DTO validation | Kotlin `data class` + kotlinx.serialization |
| pyotp | TOTP | RFC-6238 HMAC-SHA1 TOTP (any Kotlin lib or ~30 lines) |
| Docker / uvicorn | hosting | the Android process itself |

---

## 5. Groww REST Endpoint Map (the broker contract to reimplement)

Full detail also stored in repo memory at `/memories/repo/groww-rest-api-map.md`.

**Base URL:** `https://api.groww.in` — **Headers on every call:**
`Authorization: Bearer <ACCESS_TOKEN>`, `Accept: application/json`, `X-API-VERSION: 1.0`.

**Auth (token mint):** `POST /v1/token/api/access` with header `Authorization: Bearer <API_KEY>`,
body `{"key_type":"totp","totp":"<6digit>"}` → `{token, expiry, ...}`.
**Access token expires daily ~06:00 IST** → re-mint each morning with a fresh TOTP.
(Alternative "approval" flow: `checksum = SHA256(secret + epochSeconds)`.)

| `groww_client.py` method | REST endpoint | Notes |
|---|---|---|
| `place_order()` | `POST /v1/order/create` | body: trading_symbol, quantity, price?, trigger_price?, validity=DAY, exchange=NSE, segment=CASH, product=CNC\|MIS\|NRML, order_type=MARKET\|LIMIT\|SL\|SL_M, transaction_type=BUY\|SELL, order_reference_id (8–20 alnum, ≤2 hyphens). Response: `payload.groww_order_id`, `order_status`. |
| `place_gtt()` (SL exit) | `POST /v1/order/create` `order_type=SL`/`SL_M` | OCO smart order **not supported for CASH** → keep degrade-to-plain-SL behavior. (GTT single-trigger IS available for CASH if ever needed via `/v1/order-advance/create`.) |
| `delete_gtt()` | `POST /v1/order/cancel` | `{segment, groww_order_id}` |
| `get_positions()` | `GET /v1/positions/user?segment=CASH` | `payload.positions[]`: `quantity`(net), `credit_quantity`, `debit_quantity`, `net_price`, `realised_pnl`, `product` |
| `get_orders()` | `GET /v1/order/list?segment=CASH&page=0&page_size=100` | `payload.order_list[]` |
| `get_order_history()` / status | `GET /v1/order/detail/{groww_order_id}?segment=CASH` or `GET /v1/order/status/{id}?segment=CASH` | `order_status`, `filled_quantity`, `average_fill_price` |
| `get_holdings()` | `GET /v1/holdings/user` | `payload.holdings[]` |
| `get_ltp()` | `GET /v1/live-data/ltp?segment=CASH&exchange_symbols=NSE_RELIANCE,NSE_TCS` | up to **50 symbols/call**; returns `{"NSE_RELIANCE":2334.2,...}` |
| OHLCV/volume snapshot (scanner) | `GET /v1/live-data/quote?exchange=NSE&segment=CASH&trading_symbol=RELIANCE` | **quote has volume**; the `/ohlc` endpoint does **not** → use `/quote` for volume ratio. Single-symbol → 1 call/symbol. Also exposes `upper_circuit_limit` used by the order circuit-guard. |
| historical candles | `GET /v1/historical/candles?exchange=NSE&segment=CASH&groww_symbol=NSE-WIPRO&start_time=&end_time=&candle_interval=1minute` | `payload.candles[[ts,o,h,l,c,volume,oi]]`. Limits: 1min=30d, 10/15/30min=90d, 1h/4h/day/week/month=180d. (Old `/v1/historical/candle/range` is **deprecated**.) |
| instruments | `GET https://growwapi-assets.groww.in/instruments/instrument.csv` | **public, no auth**; cols incl. exchange, exchange_token, trading_symbol, groww_symbol, segment, lot_size, tick_size, buy_allowed, sell_allowed |
| user profile (optional) | `GET /v1/user/detail` | segments, nse/bse enabled |
| margin (optional) | `GET /v1/margins/detail/user` ; `POST /v1/margins/detail/orders?segment=CASH` | available cash / required margin |

**Rate limits (per group, shared):** Orders 10/s, 250/min · Live-Data 10/s, 300/min ·
Non-Trading (status/list/positions/holdings/margin) 20/s, 500/min.

**Enums:** order_status NEW/ACKED/APPROVED/REJECTED/EXECUTED/CANCELLED/COMPLETED… ·
exchange NSE/BSE/MCX · segment CASH/FNO/COMMODITY · order_type LIMIT/MARKET/SL/SL_M ·
product CNC/MIS/NRML · transaction BUY/SELL · validity DAY · candle_interval 1minute…1month.

---

## 6. Migration Gotchas (must-handle differences)

1. **Product enum mismatch.** The SDK used `INTRADAY`/`DELIVERY`; the current code passes
   `decision.product_type.value`. REST requires `MIS`/`CNC`/`NRML`. Map **MIS←INTRADAY,
   CNC←DELIVERY** at the client boundary. (The risk manager already keys MIS square-off on
   `product_type == "MIS"`, so internal values should standardize on the REST set.)
2. **Order id field name.** REST response is `groww_order_id` (not the SDK's `order_id`/`orderId`).
   The DB column is currently `kite_order_id` (legacy name) and stores `str(order_id)` — keep the
   column, store `groww_order_id` in it.
3. **Positions schema.** REST uses `credit_quantity`/`debit_quantity` + net `quantity`, **not** the
   SDK's `buy_quantity`/`sell_quantity`. State-recovery code reads `p["tradingsymbol"]` and
   `p.get("quantity")` from a normalized `{"net":[...]}` shape — the Kotlin client must normalize
   REST positions into that same shape (net qty, tradingsymbol).
4. **OCO not supported for CASH.** Current `place_gtt()` already degrades to a plain SL order — this
   is correct and must be retained. Do **not** attempt OCO smart orders for equities.
5. **No websocket.** GrowwFeed is Python-SDK-only and absent from the cURL docs. Replace with
   **REST polling** (see §7). This is the single biggest architectural change.
6. **Daily token expiry (~06:00 IST).** Re-mint via `POST /v1/token/api/access` with fresh TOTP at
   the 08:30 reauth job (and on any 401/403 mid-session, matching today's auto-reauth + circuit breaker).
7. **Quote vs OHLC for volume.** Volume ratio requires `/v1/live-data/quote` (has `volume`), not
   `/v1/live-data/ohlc` (no volume). Also source `upper_circuit_limit` from `/quote` for the
   pre-order circuit guard.
8. **Rate-limit budgeting.** Batch LTP (≤50 symbols → 1 call). At ~30 symbols polled every 1–2 s that
   is 30–60 Live-Data calls/min, well under 300/min. Per-symbol quote at 60 s cadence = ~30/min. Keep
   a shared token-bucket limiter per group.
9. **Fill verification timing.** Current code retries `get_order_history()` up to 3× (≤1.5 s) looking
   for a `COMPLETE` entry with `average_price`. REST equivalent: poll `/v1/order/detail/{id}` for
   `order_status==EXECUTED/COMPLETED` and `average_fill_price`. Preserve the retry/backoff.
10. **NSE client friction.** `nse_client.py` hits nseindia.com which requires a browser-like
    `User-Agent` and cookie priming — replicate header/cookie handling in OkHttp.

---

## 7. Live-Data Strategy: Websocket → Polling (the core change)

**Today:** Scanner subscribes to GrowwFeed websocket (LTP-only ticks) and supplements OHLCV via a
60 s REST poll. A `CandleBuilder` aggregates ticks into 1m/5m candles; indicators
(VWAP/RSI/EMA(9/21)/MACD/ATR/5m-RSI) recompute per tick; `_check_signal()` fires when **all**
conditions pass.

**On Android (poll-driven):**
- **Price loop:** every **1–2 s**, one batched `GET /v1/live-data/ltp` for all watchlist symbols
  (+NIFTY index). Feed each price into `CandleBuilder.add_tick()` exactly as a websocket tick — the
  builder is already time-bucketed, so synthetic ticks at 1–2 s cadence build identical candles.
- **Volume/OHLC loop:** every **60 s** (unchanged), `GET /v1/live-data/quote` per symbol to refresh
  the OHLCV cache + `upper_circuit_limit`. Keep the existing **freshness gate**
  (`MAX_OHLCV_STALENESS_SECS=90`) so stale volume never produces a signal.
- **Risk loop:** every **5 s**, build LTP map from the same price cache (current `_build_ltp_map`
  already prefers broker LTP). No change to rule logic.

This maps cleanly because the scanner was **already** built to tolerate a tick + periodic-REST hybrid.

---

## 8. Scheduler & Foreground-Service Design (IST day)

**Service:** a single `startForegroundService` with a persistent notification ("AutoTrader running").
Holds the orchestrator coroutine scope; must survive screen-off and Doze.

**Scheduling:** APScheduler IST cron → for each daily job compute the next
`ZonedDateTime` in `Asia/Kolkata`, convert to epoch millis, and arm
`AlarmManager.setExactAndAllowWhileIdle(RTC_WAKEUP, …, pendingIntent)`. The alarm's
BroadcastReceiver posts the job onto the service scope. Re-arm for the next day at fire time.
Use `WorkManager` as a backstop for missed/retryable work (research catch-up).

**Jobs:** 06:00 research · 08:30 reauth · 09:15 start session · 12:30 research (45-min guard) ·
15:30 stop session. Plus the in-session loops (price 1–2 s, volume 60 s, risk 5 s) run as
coroutines inside the active session, not as alarms.

**Catch-up on (re)start:** on service start during 09:15–15:29 on a non-holiday weekday, auto-start
the session and trigger a research run if no brief exists for today — identical to `main.py` today.

---

## 9. Kotlin `GrowwRestClient` Contract (target interface)

```kotlin
interface GrowwRestClient {
    // ── Auth ──
    suspend fun mintAccessToken(apiKey: String, totp: String): TokenResult   // POST /v1/token/api/access
    fun setAccessToken(token: String)

    // ── Orders ──
    suspend fun placeOrder(req: PlaceOrderRequest): PlacedOrder              // POST /v1/order/create → groww_order_id
    suspend fun cancelOrder(segment: String, growwOrderId: String)          // POST /v1/order/cancel
    suspend fun getOrderDetail(growwOrderId: String, segment: String): OrderDetail   // /v1/order/detail/{id}
    suspend fun listOrders(segment: String, page: Int = 0, size: Int = 100): List<OrderSummary>

    // ── Portfolio ──
    suspend fun getPositions(segment: String = "CASH"): List<Position>      // normalized: tradingsymbol, netQty,...
    suspend fun getHoldings(): List<Holding>

    // ── Live data ──
    suspend fun getLtp(exchangeSymbols: List<String>): Map<String, Double>  // ≤50 per call
    suspend fun getQuote(exchange: String, tradingSymbol: String): Quote     // volume, ohlc, upper_circuit_limit
    suspend fun getHistoricalCandles(growwSymbol: String, interval: String,
                                     start: String, end: String): List<Candle>

    // ── Instruments ──
    suspend fun downloadInstrumentsCsv(): List<Instrument>                    // public CSV, no auth
}
```

`PlaceOrderRequest` carries `tradingSymbol, quantity, price?, triggerPrice?, validity=DAY,
exchange=NSE, segment=CASH, product(MIS|CNC|NRML), orderType(MARKET|LIMIT|SL|SL_M),
transactionType(BUY|SELL), orderReferenceId`. `Position` is normalized to expose `tradingsymbol`
and net `quantity` so state-recovery logic ports unchanged.

---

## 10. Business Logic to Preserve EXACTLY (do not "improve")

These are tuned trading rules; the port must reproduce them verbatim.

**Scanner entry conditions (`_check_signal`, all must pass):**
1. Price > VWAP
2. Tick RSI(14) in **45–65** (scanner band; DecisionEngine accepts 40–72)
3. Volume > **1.5×** 20-day avg (skipped in paper mode)
4. EMA(9) > EMA(21) on 1m candles (≥35 candles required)
5. MACD histogram > 0 on 1m candles (≥35 candles)
6. 5-min RSI in **45–72** (HTF filter, when available)
7. Gap-at-open < `gap_filter_pct` (1.5%)
- Hard cutoff 15:15; Friday no signals after 14:00; per-symbol cooldown; OHLCV freshness gate;
  NIFTY intraday trend filter modulated by bias (BULLISH −0.8% / NEUTRAL −0.5% / BEARISH −0.3%).

**Decision engine bias × stance matrices** (`decision_engine.py` ~L596–605):
- `max_trades` = {(BULLISH,FULL):10, (BULLISH,HALF):6, (NEUTRAL,FULL):`max_trades_per_day`(6),
  (NEUTRAL,HALF):4, (BEARISH,*):4}
- `max_positions` = {(BULLISH,FULL):5, (BULLISH,HALF):3, (NEUTRAL,FULL):`max_open_positions`(3),
  (NEUTRAL,HALF):2, (BEARISH,*):2}
- (These already feed the dashboard via `effective_max_trades_per_day` / `effective_max_open_positions`.)

**Risk manager rule order per 5 s poll (per open trade):** SL hit → partial booking → ROI decay →
target → trailing SL → MIS 15:00 square-off; then daily drawdown halt; then EOD report.
- ROI decay tiers: >20 min→1.5%, >35 min→1.0%, >50 min→ATR floor `max(0.5%, 0.4×ATR/entry)`,
  never below trailing SL or above target.
- Trailing SL: activate at `+0.8%`, trail `0.7%` below LTP, only moves up.
- Partial booking: at `entry + R×1.0` (R = entry − initial stop), book 50%, move SL to breakeven.
- Drawdown: 2% soft alert, **3% hard halt**. Consecutive-loss pause: 3 SLs → 30 min pause.

**Order placement sequence (`trading_agent.py`):** circuit-limit guard (quote `upper_circuit_limit`,
skip if LTP ≥ 99.5% of upper) → place MARKET BUY → verify fill (retry ≤1.5 s) → recompute SL/target
from fill (ATR-based, else fixed 1.0%/2.0%) → place GTT/SL exit (on failure: **HALT new trades** +
Telegram) → compute partial target → persist Trade → increment counter (EXECUTE only) → publish event
→ Telegram entry alert.

**Capital model:** total ₹10,00,000; buckets core 70% / hedge 20% / warchest 10%; max loss/trade 1.5%;
max 3 open positions / 6 trades-per-day baseline (scaled by matrices); max 2 trades/symbol/day.

---

## 11. State & Persistence Migration

**Redis KV → DataStore (persistent) + in-memory:**
| Redis key | New home |
|---|---|
| `trading_halt`, `groww_session_token`, `daily_trade_count` | DataStore (survive restart) |
| `agent:*:status`, `agent:research:*`, `agent:trading:last_signal_*`, `agent:risk:*` | in-memory StateFlow (reset on restart, like today) |
| `latest_market_brief`, `today_watchlist`, `groww_instrument_map` | DataStore/Room (with 24 h TTL semantics) |
| `decision_feed` (rolling 100, LPUSH/LTRIM) | bounded in-memory `ArrayDeque(100)` + StateFlow |
| `trade_atr:{symbol}` (24 h TTL) | in-memory map with expiry timestamps |
| `data_api:*`, `scanner:feed_connected_at` | in-memory StateFlow |
| `anthropic_calls:today:*` (24 h TTL) | in-memory counters reset at IST midnight |

**Redis pub/sub channels → EventBus (`MutableSharedFlow`):** `trade_events`, `eod_report`,
`market_brief`, `system_alerts`, `decision_feed`, plus the 2 s `ltp_update` snapshot. The Ktor
WebSocket subscribes to these flows and forwards them to the dashboard unchanged.

**PostgreSQL → Room entities/DAOs:** `Trade` (incl. `kite_order_id`, `partial_target_price`,
`gtt_trigger_id`, status OPEN/CLOSING/CLOSED, trade_date/entry_time), `MarketBrief`
(unique per date), `DailyPnl`, `AgentLog`. Port the 3 Alembic migrations as the initial Room schema
(no need for incremental migrations on day one).

---

## 12. Frontend Reuse Strategy

- Build the Angular app (`npm run build -- --configuration production`) and bundle
  `dist/autotrader-dashboard/browser/` into `app/src/main/assets/www/`.
- Local Ktor server serves those static files and implements the `/api/*` routes the dashboard
  calls (market brief, trades, pnl, system/agent status, auth control) reading from Room/DataStore/
  StateFlow, plus `/ws/live`.
- WebView (`MainActivity`) loads `http://127.0.0.1:<port>`. `proxy.conf.json` is dev-only and
  irrelevant on-device. Confirm the Angular API base URL works against the local origin (likely
  same-origin relative paths — verify).
- This keeps charts, alerts, open-positions, trade-log, pnl-chart components **unchanged**.

---

## 13. Primary Risks & Mitigations

1. **OxygenOS killing the background app (THE #1 non-code risk).** OnePlus/OxygenOS aggressively
   kills background processes. Mitigations: foreground service + persistent notification;
   `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`; disable "Sleep standby optimization" / "Intelligent
   control" for the app; lock the app in recents; consider `WorkManager` periodic heartbeat to
   restart the service; test that the 09:15–15:30 loop survives a full screen-off session.
   **This must be validated by PoC before any real porting effort.**
2. **Daily Groww token + TOTP on-device.** Verify the TOTP `POST /token/api/access` flow works
   headlessly each morning without a manual "approval" step; if approval is required, surface a
   notification prompt.
3. **Clock/timezone correctness.** All scheduling in `Asia/Kolkata`; ensure exact alarms fire in Doze
   (`setExactAndAllowWhileIdle`) and handle device reboot (`BOOT_COMPLETED` receiver re-arms alarms).
4. **Network flakiness on mobile.** Existing retry/backoff + circuit-breaker patterns must be ported;
   degrade gracefully on cellular handoff.
5. **Indicator parity.** pandas/numpy vs Kotlin rounding differences could shift a borderline signal.
   Validate indicators against the Python output on identical candle data.
6. **Secrets on device.** Store API keys/TOTP secret in EncryptedSharedPreferences/Keystore.

---

## 14. Phased Roadmap

**Phase 0 — De-risking PoC (do this FIRST).**
- Minimal foreground service that runs a 5 s loop for a full 09:15–15:30 window with screen off on
  the actual OnePlus Nord, logging heartbeats → proves background survival.
- A tiny Kotlin `GrowwRestClient` that mints a token (TOTP) and pulls batched LTP → proves auth +
  live data on-device.
- **Gate:** if background survival fails even with all exemptions, reconsider approach before more work.

**Phase 1 — Broker + data core.** Full `GrowwRestClient` (orders, positions, quote, historical,
instruments CSV); poll-driven price/volume loops; instrument map + fallback.

**Phase 2 — Trading brain.** Port Scanner (indicators + `_check_signal`), DecisionEngine (pre-checks
+ matrices + Anthropic REST), RiskManager (full rule order), TradingOrchestrator (order sequence +
state recovery), Room persistence.

**Phase 3 — Scheduler + research.** AlarmManager/WorkManager IST jobs; ResearchAgent (Alpha Vantage,
NSE, news RSS, Claude → brief + watchlist); Telegram alerts; catch-up logic.

**Phase 4 — Dashboard.** Embed Ktor server + bundled Angular build in WebView; implement `/api/*` +
`/ws/live`; verify all dashboard panels.

**Phase 5 — Parity validation.** Run paper mode on-device for several sessions; compare signals,
decisions, P&L, and dashboard state against the Python system on identical data.

---

## 15. Open Questions / To Verify

- [ ] Does Groww's TOTP token mint work fully headless daily, or is a manual approval ever required?
- [ ] Exact Angular API base-URL configuration — confirm it uses relative/same-origin paths so the
      local server origin works without a frontend change.
- [ ] Confirm `/v1/live-data/quote` returns `upper_circuit_limit` (used by the order guard); if not,
      find the field that does.
- [ ] OxygenOS background-survival result on the specific OnePlus Nord model + Android version.
- [ ] Whether to keep Postgres-style `DailyPnl`/`AgentLog` or simplify on-device.
- [ ] Anthropic/Alpha Vantage/NSE rate & cost behavior from a mobile IP.

---

## 16. Quick-Reference Pointers (source files)

- Startup/schedule/catch-up: `backend/main.py`
- Cron helpers: `backend/core/scheduler.py`
- All tunables: `backend/core/config.py`
- Redis keys & channels: `backend/core/redis_keys.py`, `backend/api/websocket.py`
- Session lifecycle: `backend/agents/trading_agent_manager.py`
- Order sequence / state recovery: `backend/agents/trading_agent.py`
- Indicators + signal: `backend/agents/scanner.py` (`_check_signal`, `compute_*`, `CandleBuilder`)
- Pre-checks + matrices + LLM: `backend/agents/decision_engine.py`
- Risk rules: `backend/agents/risk_manager.py` (`_poll`)
- Research brief: `backend/agents/research_agent.py`
- Broker wrapper (reference for REST mapping): `backend/integrations/groww_client.py`
- Instrument map + fallback tokens: `backend/integrations/instrument_service.py`
- Groww REST detail (memory): `/memories/repo/groww-rest-api-map.md`
