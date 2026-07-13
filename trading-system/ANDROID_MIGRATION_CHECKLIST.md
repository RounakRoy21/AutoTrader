# AutoTrader Android Migration — Measurable Checklist

> Companion to [ANDROID_MIGRATION_PLAN.md](ANDROID_MIGRATION_PLAN.md). The plan is the *what/why*;
> this file is the *done/not-done* tracker with **objective, testable acceptance criteria** per item.
> Every box must be checkable by a concrete observation (a log line, a screenshot, a passing test,
> a number) — never by opinion.
>
> **Branch:** `feature/android_migration`
> **Target device:** OnePlus Nord (OxygenOS)
> **Rule:** Do not start Phase N+1 until every **GATE** item in Phase N is checked.

---

## Legend

- `[ ]` not started / not verified
- `[~]` in progress
- `[x]` done AND verified against its acceptance criterion
- **GATE** = a hard stop; the phase fails if this is not met
- **Evidence** = what you must observe to check the box

---

## Phase 0 — De-risking PoC  *(prove the two riskiest unknowns before any porting)*

Goal: prove (a) the app survives a full trading session in the background on the Nord, and
(b) a Kotlin client can authenticate to Groww and pull live data on-device.

### 0.1 Project scaffold
- [x] Gradle Android project builds a debug APK
  - **Evidence:** `./gradlew :app:assembleDebug` exits 0; `app-debug.apk` produced (~7.4 MB, verified on build machine 2026-07-06).
- [ ] APK installs and launches on the OnePlus Nord
  - **Evidence:** app icon opens `MainActivity` without crash (logcat shows no fatal exception).

### 0.2 Background survival (THE #1 risk) — **GATE**
- [ ] Foreground service starts with a persistent notification
  - **Evidence:** notification "AutoTrader running" visible in the shade; `dumpsys activity services | findstr TradingService` shows it running.
- [ ] Heartbeat loop logs every 5 s with an IST timestamp
  - **Evidence:** `adb logcat -s AutoTrader` shows `heartbeat #N @ HH:mm:ss IST` at ~5 s cadence.
- [ ] **GATE:** service survives a continuous **09:15→15:30 IST window, screen off**, on battery
  - **Evidence:** heartbeat count at 15:30 ≈ expected (`(6h15m)/5s ≈ 4500 ±1%`); **no gap > 30 s** in the heartbeat log across the whole window.
  - **Evidence:** repeated on a second day to rule out a one-off.
- [ ] Battery-optimization exemption flow works
  - **Evidence:** `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` dialog appears; after granting, `dumpsys deviceidle whitelist | findstr <pkg>` lists the app.
- [ ] Survives Doze
  - **Evidence:** after `adb shell dumpsys deviceidle force-idle`, heartbeats continue (gap ≤ the maintenance-window interval, documented).
- [ ] Foreground-service type validated for a >6 h session
  - **Evidence:** using `specialUse` FGS type, session runs past 6 h with **no** system `Time limit exhausted` kill in logcat. (Documents whether `dataSync` would have been killed.)
- [ ] Re-arm after reboot (optional for PoC, required later)
  - **Evidence:** `adb reboot`; `BOOT_COMPLETED` receiver restarts the service (heartbeat resumes without opening the app).

### 0.3 Groww auth + live data on-device — **GATE**
- [x] TOTP generator produces the same 6-digit code as the Python `pyotp` for the same secret+time
  - **Evidence:** `TotpTest` passes RFC 6238 SHA1 vectors (same algorithm as `pyotp`); `:app:testDebugUnitTest` green on build machine 2026-07-06. Still confirm once with the real Groww secret on-device.
- [ ] Token mint succeeds from the device
  - **Evidence:** `POST /v1/token/api/access` returns HTTP 200 with a non-empty token; logged as `token minted, len=NN`.
- [ ] **GATE:** batched LTP fetch returns live prices for the focus universe
  - **Evidence:** `GET /v1/live-data/ltp?...exchange_symbols=NSE_RELIANCE,...` returns 200 with a numeric price per symbol; shown in the UI.
- [ ] Token expiry / re-mint path verified
  - **Evidence:** with a stale token, a 401 triggers a re-mint and the retried call succeeds (logged).
- [ ] Mobile-IP behaviour documented
  - **Evidence:** note any rate-limit/geo differences observed vs the server; no `403 forbidden` on Live-Data group.

### Phase 0 exit criteria — **GATE (all must hold)**
- [ ] Background survival GATE (0.2) green on **two** separate full-session days.
- [ ] Groww LTP GATE (0.3) green on-device.
- [ ] Decision recorded in this file: **PROCEED** to Phase 1, or **RETHINK** approach.

---

## Phase 1 — Broker + data core

### 1.1 GrowwRestClient (full contract from plan §9)
- [ ] `placeOrder` → `POST /v1/order/create`, reads `groww_order_id`
  - **Evidence:** unit test asserts request body maps `product` MIS←INTRADAY / CNC←DELIVERY and parses `payload.groww_order_id`.
- [ ] `cancelOrder` → `POST /v1/order/cancel`
- [ ] `getOrderDetail` → `/v1/order/detail/{id}` parses `order_status`, `filled_quantity`, `average_fill_price`
- [ ] `getPositions` normalizes REST `credit_quantity`/`debit_quantity`/net → `{tradingsymbol, netQty}`
  - **Evidence:** unit test feeds a sample REST positions payload and asserts the normalized shape state-recovery expects.
- [ ] `getLtp` batches ≤ 50 symbols/call
  - **Evidence:** test with 60 symbols issues exactly 2 calls.
- [ ] `getQuote` parses `volume` + `upper_circuit_limit`
- [ ] `getHistoricalCandles` parses `payload.candles[[ts,o,h,l,c,v,oi]]`
- [ ] `downloadInstrumentsCsv` parses public CSV → symbol→exchange_token map
  - **Evidence:** map size > 0; RELIANCE resolves to the expected token; `FALLBACK_TOKEN_MAP` retained.

### 1.2 Rate limiting & resilience
- [ ] Shared token-bucket per group (Orders 10/s·250/min, Live-Data 10/s·300/min, Non-Trading 20/s·500/min)
  - **Evidence:** load test never exceeds the per-minute budget (counter assertion).
- [ ] Retry/backoff + 60 s circuit-breaker ported (matches `groww_client.py`)
  - **Evidence:** simulated outage > 60 s sets HALT + emits alert; recovery clears it.

### 1.3 Poll-driven live data (plan §7)
- [ ] Price loop: batched LTP every 1–2 s feeds `CandleBuilder.addTick`
  - **Evidence:** with a 2 s cadence, 1 m candles built over 10 min match a reference set within tick granularity.
- [ ] Volume/OHLC loop: `getQuote` per symbol every 60 s refreshes cache + `upper_circuit_limit`
- [ ] Freshness gate `MAX_OHLCV_STALENESS_SECS = 90` enforced
  - **Evidence:** stale (>90 s) volume yields **no** signal (test).

---

## Phase 2 — Trading brain (port EXACTLY — plan §10)

### 2.1 Indicators (parity, not reinvention)
- [ ] VWAP, RSI(14) tick-based, EMA(9/21), MACD histogram, ATR, 5-min RSI
  - **Evidence:** on an identical candle fixture, Kotlin indicator outputs match the Python outputs within a documented epsilon (e.g. ≤ 1e-6 relative).
- [ ] Golden-file parity test committed
  - **Evidence:** a shared CSV of candles + expected indicator values passes in CI.

### 2.2 Scanner `_check_signal` — all 7 conditions
- [ ] Price>VWAP · tick-RSI 45–65 · Volume>1.5× · EMA9>EMA21 (≥35 candles) · MACD>0 · 5m-RSI 45–72 · gap<1.5%
- [ ] Guards: 15:15 cutoff · Friday no-signal-after-14:00 · per-symbol cooldown · OHLCV freshness · NIFTY trend filter (BULLISH −0.8% / NEUTRAL −0.5% / BEARISH −0.3%)
  - **Evidence:** table-driven tests, one per condition, asserting a borderline pass and fail.

### 2.3 Decision engine
- [ ] Pure-Kotlin pre-checks (RSI 40–72, Vol ≥1.5×, VWAP dev 0–1.5%, R:R ≥ 2.0)
- [ ] Bias × stance matrices reproduce `max_trades` / `max_positions` exactly
  - **Evidence:** parametrized test over all (bias, stance) pairs asserts the plan §10 numbers.
- [ ] Anthropic REST call; `SignalAudit` overwrites factual fields from ground truth
  - **Evidence:** test injects a wrong LLM RSI and asserts it is overwritten before validation.

### 2.4 Risk manager (5 s poll, exact rule order)
- [ ] Order: SL → partial booking → ROI decay → target → trailing SL → 15:00 square-off → drawdown halt → EOD
- [ ] ROI decay tiers (>20→1.5%, >35→1.0%, >50→ATR floor `max(0.5%,0.4×ATR/entry)`), never below trail / above target
- [ ] Trailing (activate +0.8%, trail 0.7%, up-only) · partial (1R, 50%, SL→breakeven) · drawdown (2% soft, 3% hard) · 3-SL→30 min pause
  - **Evidence:** scenario tests drive an open position through each branch and assert the transition.

### 2.5 Trading orchestrator (order sequence)
- [ ] circuit guard (skip if LTP ≥ 99.5% upper) → MARKET BUY → fill verify (retry ≤1.5 s) → recompute SL/target → SL exit (fail→HALT+alert) → partial target → persist → counter (EXECUTE only) → event → alert
  - **Evidence:** end-to-end test in paper mode reproduces the exact call order (spy/mock assertions).

### 2.6 Room persistence
- [ ] Entities: Trade (incl. `kite_order_id`, `partial_target_price`, `gtt_trigger_id`, status, dates), MarketBrief (unique/date), DailyPnl, AgentLog
  - **Evidence:** schema export matches the 3 Alembic migrations' columns; insert/query round-trips.

---

## Phase 3 — Scheduler + research

- [ ] AlarmManager `setExactAndAllowWhileIdle` fires each IST job within ±5 s
  - **Evidence:** logcat shows 06:00 / 08:30 / 09:15 / 12:30 / 15:30 fire at the right IST instant; monthly NIFTY-50 check on the 1st @ 05:30.
- [ ] WorkManager backstop retries a missed/failed research run
  - **Evidence:** kill the alarm mid-run; WorkManager re-runs and a brief is produced.
- [ ] Catch-up on restart during 09:15–15:29 auto-starts the session + research if no brief today
  - **Evidence:** launch at 11:00 on a trading day → session ACTIVE and a brief exists (matches `main.py`).
- [ ] ResearchAgent ports all sources (Yahoo, NSE, RBI RSS, news RSS, Alpha Vantage fallback) → brief
  - **Evidence:** a produced brief has bias, VIX regime, watchlist, and news flags populated.
- [ ] Reboot receiver re-arms all alarms
  - **Evidence:** after reboot, `dumpsys alarm | findstr <pkg>` lists the day's alarms.

---

## Phase 4 — Dashboard (reuse Angular unchanged)

- [ ] Angular production build bundled into `assets/www`
  - **Evidence:** APK contains `assets/www/index.html`; size logged.
- [ ] Embedded Ktor server serves the SPA on `127.0.0.1:<port>`
  - **Evidence:** WebView loads the dashboard; no 404s in logcat.
- [ ] `/api/*` routes the UI calls are implemented from Room/DataStore/StateFlow
  - **Evidence:** each panel (P&L, positions, trade log, decision feed, system status, market brief) renders real data.
- [ ] `/ws/live` bridges the EventBus + 2 s LTP snapshots
  - **Evidence:** live tick updates and trade/decision events appear without refresh.
- [ ] **Zero frontend source changes**
  - **Evidence:** `git diff trading-system/frontend/src` is empty for the port.

---

## Phase 5 — Parity validation

- [ ] Paper mode runs a full session on-device without crash
  - **Evidence:** 09:15–15:30 completes; EOD report generated.
- [ ] Signal parity vs Python on identical data
  - **Evidence:** replay a captured tick/quote set through both; signal set matches (same symbols, same minute) within a documented tolerance.
- [ ] Decision parity (pre-check outcomes)
  - **Evidence:** deterministic pre-check verdicts match Python for the same signals (LLM step compared separately).
- [ ] P&L / counter parity
  - **Evidence:** paper trades, realized P&L, `daily_trade_count`, and `avg_realised_rr` match the Python run for the same inputs.
- [ ] Multi-day soak
  - **Evidence:** ≥ 5 consecutive on-device paper sessions with no fatal, no missed square-off, no stuck HALT.

---

## Cross-cutting (track continuously)

- [ ] Secrets in EncryptedSharedPreferences / Keystore (never plaintext)
  - **Evidence:** no API key/TOTP secret in logs, prefs XML, or the APK.
- [ ] All scheduling uses `ZoneId.of("Asia/Kolkata")`
  - **Evidence:** grep shows no `LocalDateTime.now()` without an IST zone in scheduling code.
- [ ] NSE holiday guard ports and skips the whole day
  - **Evidence:** on a holiday date, no session starts (test with a mocked date).
- [ ] Indicator epsilon documented and asserted in CI.
