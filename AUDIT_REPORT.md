# AutoTrader Audit (2026-11-14)

# AutoTrader Audit — Part A: Market & Business-Logic Audit

*Read-only review. No files were changed. Where the researcher and verifier disagreed, this report uses the verifier's corrected severity and figures. Items labelled **not independently verified** come from one lens only and were not re-checked against the code.*

---

## 1. What the system is

1. AutoTrader is a single-user intraday trader for NSE cash equities. It is long-only and uses MIS. Groww is the live broker, and Kite is wired up but not used. It runs on FastAPI + Postgres + Redis, with an Angular 17 dashboard.
2. Each morning at 06:00, a Sonnet "market brief" sets the day's bias, stance, watchlist and avoid list. It is refreshed at 12:30. The brief decides which stocks the scanner looks at, and it can switch trading off for the whole day.
3. The scanner reads GrowwFeed LTP ticks plus a 60 s REST OHLCV poll. A signal needs price above VWAP, an RSI band, a volume ratio, EMA9>EMA21, MACD>0, a gap filter, a NIFTY trend filter and a 5m RSI check.
4. Haiku scores each signal. The code then recomputes SL (1.5×ATR), target (3×ATR) and qty itself. A 5 s risk-manager poll handles the SL, target, 1R partial, breakeven, trailing stop, ROI decay and the 15:00 MIS flatten. A broker-side SL order is supposed to back it up.
5. It defaults to paper mode (`paper_trading=True`). It can be deployed to an Oracle VM or to Android/Termux. `backend/backtest/runner.py` is a separate copy of the scanner rules, used only for replay.

My one-line verdict as a trader: the design idea is sound. Rules wrap the LLM, and code owns stops and sizing. That is better than most retail LLM bots. **But the live execution path can open unintended naked MIS shorts, and the signal layer measures different things from what its names say. Do not put real capital on it in its current state.**

---

## 2. Competitive landscape and where we lag

### 2.1 Landscape (Indian and global, merged; prices are "reported" and may be out of date)

| Product | Category | Strengths relevant to us | Backtest | Risk / execution | AI/LLM | Pricing (reported) |
|---|---|---|---|---|---|---|
| [Zerodha Streak](https://www.chittorgarh.com/article/zerodha-streak-review-algo-trading-for-retail-investors/516/) | No-code indicator builder + scanner (Zerodha only) | Scans the whole market, alerts, semi-auto confirmation | Multi-symbol, with DD/P&L stats | Strategy SL/target, virtual (paper) deploy | None | Core free since 2024; older tiers ₹350–1,400/mo |
| [Kite Connect / Kite MCP](https://github.com/zerodha/kite-mcp-server) | Broker API + official MCP | Full-mode ticks (volume, OHLC, depth) | Historical data on the paid tier | GTT-OCO, market protection on by default | Hosted MCP is read-only (except GTT) | Personal free; Connect ₹500/mo ([charges](https://support.zerodha.com/category/trading-and-markets/kite-api/articles/what-are-the-charges-for-kite-apis)) |
| [Tradetron](https://help.tradetron.tech/en/article/tradetron-pricing-plans-upgrade-refund-policy-7oisc0/) | Cloud no-code, marketplace, multi-broker | Stockbags, multiple strategies, empanelled hosting | Basic–moderate | Strategy MTM SL/target, exit-all | None | Free → ~₹300–1,000/mo; Creator ~$300/mo |
| [AlgoTest](https://algotest.in/blog/8-best-algo-trading-platforms-in-india-2026/) | Options-first visual builder | Multi-leg, portfolio strategies | Strong: multi-year, portfolio | Portfolio SL/target, Greeks, margin | "Signals AI" builds, backtests and deploys from a text description | ₹499–5,999 credit packs |
| [uTrade Algos](https://www.softwareadvice.com/product/530216-uTrade-Algos) | Multi-asset retail algo | Prebuilt + custom strategies | Up to 36 months | Concurrent-algo caps, strategy SL | "uTrade Intelligence" | ~₹999/mo; ₹14,999 tier |
| [AlgoBulls](https://algobulls.com/blog/industry-insights-and-updates/sebi-new-algotrading-regulations-for-retail-investors-2026) | Marketplace + Python SDK | No-code / AI / Python | Backtest + paper | Platform-level | AI Copilot | Plan-based |
| Quantiply | Automated F&O execution | Templates, multi-broker | Limited | Live strategy P&L | None | ~₹250–2,000/mo |
| [Stratzy](https://stratzy.in/blog/stratzy-pricing-subscription-india-2025) | Done-for-you SEBI-RA strategies | Curated | Vendor-run | Vendor-managed | Marketed as AI/quant | ~2% of capital/yr or subscription |
| [Sensibull](https://algotest.in/blog/sensibull-review-2026-features-pricing-and-who-its-for/) | Options analytics | OI, payoff, Greeks | None | Basket orders | None | Free/Pro by broker |
| [Dhan / DhanHQ](https://dhanhq.co/docs/v2/traders-control/) | Broker API + trader controls | Skill packs for AI coding agents | Historical API | **[Kill Switch API](https://madefortrade.in/t/introducing-kill-switch-api-on-dhanhq-trading-apis-for-managing-risk/25002)**, **[P&L auto-exit, Exit-All](https://madefortrade.in/t/now-live-p-l-based-exit-and-exit-all-on-apis/64092)**, Super Orders (entry+SL+target) | Agent skill packs | API free |
| [Angel One SmartAPI](https://www.angelone.in/news/angel-one-smartapi-the-secret-to-easy-automated-algo-trading) | Free broker API | ARQ Prime (rule-based) | Free historical data | GTT; no plain market orders for algos | Rule-based | Free |
| Upstox / [Fyers](https://fyers.in/api-docs/v3/api/trading/order-placement/gtt-orders/gtt-oco-order/) / Kotak Neo | Broker APIs | — | Historical APIs | GTT-OCO (Fyers). Market orders converted to MPP (Fyers) or disallowed for retail algo (Kotak) | None | Upstox ~₹750/mo; others free or low |
| **[Groww Trade API](https://groww.in/trade-api/docs/python-sdk) (our broker)** | Broker API + WS (1,000 subscriptions, LTP/index/depth/order updates) | — | Historical candles | **[Smart Orders: GTT + OCO](https://groww.in/trade-api/docs/python-sdk/smart-orders)**. The SDK docs say CASH OCO works for MIS; the [cURL docs](https://groww.in/trade-api/docs/curl/smart-orders) say it does not. **Check this before relying on it.** Limits: orders 10/s, 250/min; live data 10/s, 300/min; token endpoint 150/24h | None | ~₹499/mo or ₹4,999/yr ([ref](https://onetradejournal.com/trading-apis)) |
| [Trade Ideas Holly](https://www.trade-ideas.com/trade-ideas-review/) | US intraday signals | Re-backtests 60+ strategies every night; only recent winners are allowed to trade | OddsMaker | Entry/stop/target on every signal | Holly AI | — |
| TrendSpider / [Tickeron](https://walnutinvest.com/comparison/tickeron-vs-trendspider) | Automated TA + backtesting / pattern odds | Multi-timeframe; publishes measured hit rate for each signal type | Strong replay | Alerts to broker | Pattern AI | — |
| Composer | No-code US automation | AI writes the strategy, a deterministic engine runs it; **same logic for backtest and live** | Yes | Platform | AI authoring | — |
| [QuantConnect LEAN](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/key-concepts.md) | Open-source engine | Pluggable fill, slippage, fee and margin models; same code for backtest, paper and live | Yes | `Liquidate()`, brokerage sync | — | OSS |
| [Alpaca](https://docs.alpaca.markets/us/docs/working-with-orders) | US broker API | Native bracket/OCO/OTO and server-side trailing stops | — | Broker-held exits | — | — |
| Kavout | ML stock ranking | Ranks the whole universe cross-sectionally | — | — | ML | — |
| [TradingAgents](https://arxiv.org/abs/2412.20138v1) / [FinMem](https://arxiv.org/pdf/2311.13743) / FinRobot / FinGPT | Research LLM agents | Bull/bear debate, LLM risk team, layered memory, reflection | Short windows; possible look-ahead | — | Core | OSS |
| [FINSABER](https://arxiv.org/abs/2505.07078), [LiveTradeBench](https://arxiv.org/html/2511.03628v1), StockBench, memorisation studies ([1](https://hedgefundalpha.com/education/your-llms-alpha-might-be-mere-memorization/), [2](https://arxiv.org/pdf/2512.23847v1), [3](https://arxiv.org/abs/2605.24564)) | Evaluation and critique | LLM gains "deteriorate significantly" over longer horizons. LLMs are too cautious in bull markets and too aggressive in bear markets. Only post-cutoff, forward data counts as evidence. | — | — | — | — |

### 2.2 Where we lag (deduplicated, in order of importance)

1. **Exits held at the broker.** Every serious platform holds exits at the broker: Groww OCO, Dhan Super Orders, Kite and Fyers GTT-OCO, Alpaca brackets and trailing stops. We place one SL-limit leg, lose its ID, and trail only in the DB.
2. **Kill switch that flattens.** Dhan has Kill Switch, P&L exit and Exit-All; Tradetron and AlgoTest have MTM SL; LEAN has `Liquidate()`. Our HALT only blocks new entries, and a restart clears it.
3. **Reconciliation with the broker.** Hosted platforms and LEAN sync from the order-update stream. We reconcile once, at startup, and in one direction only.
4. **A backtest that is the live code and includes costs.** Composer, LEAN, AlgoTest and uTrade all do this. Our runner is a hand copy that has drifted, has no costs, and does not simulate our exits.
5. **Ongoing proof of edge.** Holly re-validates nightly and Tickeron publishes hit rates. We have no walk-forward test, no per-setup statistics and no decay monitoring.
6. **Market data quality.** Kite full mode and the Groww/Dhan depth feeds carry volume, OHLC and timestamps. We read LTP only and get volume from a 60 s REST poll.
7. **Universe selection.** Streak scans the whole market, Trade Ideas covers 8,000 names, and Kavout ranks everything. We trade 6 mega-caps or an LLM watchlist of 10 or fewer.
8. **Measuring the LLM.** No Indian retail product does this. It would be a real differentiator *if* we log decisions and A/B test them. Today we do neither.
9. **Shorts and regime adaptation.** Streak, Tradetron and AlgoTest support both. We are long-only and go idle on any BEARISH label.
10. **Calendar and events.** Every broker platform ships an exchange calendar. Our 2026 holiday set is empty.

---

## 3. Regulatory and compliance exposure

Sources: [SEBI Sep-2025 circular](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf), [Zerodha overview](https://zerodha.com/z-connect/general/a-comprehensive-overview-of-nses-circular-on-the-new-retail-algo-trading-framework), [Fyers Apr-2026 rules](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026), [Marketcalls](https://www.marketcalls.in/market-regulations/new-nse-rules-for-retail-algo-trading-what-retail-traders-algo-platforms-and-brokers-must-now-do.html), [Kotak Neo](https://www.kotakneo.com/investing-guide/articles/retail-algo-trading-rules-nse-circular/), [HDFC Sec (NSE/INVG/67858)](https://www.hdfcsec.com/blog/details/safer-participation-in-algorithmic-trading-important-update-on-nse-circular-125608), [Groww blog](https://groww.in/blog/nse-issues-new-compliance-norms-for-retail-algo-trading), [Groww static IP](https://groww.in/blog/static-ip-api-trading-setup), [Kite forum on market orders](https://kite.trade/forum/discussion/comment/50579). The researchers did not read the primary NSE circular PDFs, so treat the exact wording (OPS per client vs per segment, how often the IP can change) as unconfirmed.

**Framework status.** SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 (4 Feb 2025) and NSE standards NSE/INVG/67858. After several delays, CIR/2025/132 made the framework **mandatory for all brokers from 1 Apr 2026**, so it is in force today.

| Requirement | Our status | Exposure |
|---|---|---|
| **Self-use route below 10 OPS.** No exchange registration needed; the broker tags orders with the Algo ID. | Config caps us at 6 trades/day and 3 open positions, so we are far below the limit. | **Compliant.** No throttle is legally needed at this volume. A limiter is still worth having for Groww's API limits. |
| **Static IP for order placement** (1 primary + optional secondary; changeable about weekly) | No startup check. **The Android/Termux path runs on a dynamic mobile or home IP.** The Oracle VM needs a reserved IP whitelisted in Groww. | **Live blocker on Termux.** Orders from that path will be rejected. The verifier confirmed the failure is loud (`place_order` raises), not silent. |
| **Algo ID tagging** | The broker adds it for self-built algos. We send only `source="autotrader"`. | No code change is required. Send `order_reference_id` (8–20 alphanumeric) anyway for traceability and idempotency. |
| **Daily logout + 2FA per day; no continuous refresh-token sessions** | The 08:30 TOTP re-login fits. Per research, the Redis token has no TTL and `token_refresh.py` is a stub. | Low. Expire the token explicitly at end of day and handle forced logout. |
| **Market orders: MPP / protection / bans** | Every entry and exit is MARKET. There is no check of `filled_quantity` and no handling of rejections. | **High in practice.** Broker-side conversion or rejection combined with our missing fill checks (see §4.2) leaves positions unmanaged. |
| **OAuth/2FA on API access; open-access APIs banned** | Per research: `admin_api_key=""` by default. `POST /api/trades/{id}/close` (a live market SELL), `POST /api/auth/groww/login` and `POST /api/market-brief/run` have no auth. The Oracle deploy opens port 80 with no TLS. | **High security risk.** Anyone who can reach the box can close positions or spend Sonnet credit. It also goes against the framework's intent. |
| **Black-box / provider rules** (RA registration, empanelment, Indian hosting, 5-year logs) | Personal use only. | Keep it strictly single-user. Sharing the bot or its LLM signals, even with friends, makes it a black-box algo that needs RA registration and empanelment. Static IPs can be shared only with family, with written consent. |
| **Audit trail** | Decisions go only to a Redis list capped at 100. Exit and SL broker order IDs are not stored (see §4.2). | Not mandatory for a personal user, but you need an immutable order and decision log to reconcile against contract notes and for tax. |

**Context from the SEBI study ([PR 37/2024](https://www.sebi.gov.in/media-and-notifications/press-releases/jul-2024/sebi-study-finds-that-7-out-of-10-individual-intraday-traders-in-equity-cash-segment-make-losses_84948.html)).** 71% of intraday equity-cash traders lost money, rising to 80% for those making more than 500 trades a year. Loss-makers paid costs equal to a further 57% of their losses. For a ₹1L account sized at about ₹33k per position, costs are not a rounding error. They decide whether the strategy makes money.

---

## 4. Trader's audit

**Severity key.** Severity is the verifier's final rating. *Verified* means the finding was traced in code by a second pass. ⚠ **not independently verified** means it came from one lens only.

**Cost reference used throughout.** Running the verifier's own `_transaction_costs` on ₹33k each way gives about **₹35.3 per round trip (~0.107%)**: brokerage 19.8, STT 8.25, exchange 2.28, GST 3.97, stamp 0.99, SEBI 0.07. One lens said ₹25 / 0.075%. That figure is wrong and is superseded.

### 4.1 Edge and signals

| # | Sev | Where | Finding | Why it costs money | Fix |
|---|---|---|---|---|---|
| E1 | **High** (verified) | `agents/decision_engine.py:814-824`, `:849-912`; `agents/scanner.py:345-366` | **Stops and targets come only from a 1-minute ATR, and the R:R gate cannot reject anything.** SL = ltp − 1.5×ATR(1m) and target = ltp + 3×ATR(1m), whenever there are 15 or more candles. `actual_rr` is therefore always 3.0/1.5 = 2.0, so `RR_MIN=2.0` never rejects. There is no minimum stop distance, and costs are never part of the decision. A further mismatch: sizing ignores ATR before 45 candles, but the SL uses ATR from 15 candles onward. | With a 1m ATR of about 0.12% on a large cap, the SL is about 0.18% and the target about 0.36%. Net of 0.107% costs, R:R is about **0.87:1**, so you need roughly a 53% win rate before slippage. At a 0.25% ATR it is about 1.35:1. The stop sits inside bid/ask noise, and the "2:1" in the audit log is a tautology. The verifier calls this a severe design weakness, not proven negative expectancy. | Base the stop on 5m/15m ATR or 0.25–0.4× daily ATR14, with a floor of about 0.4–0.5%. Gate on **net** R:R ≥ 1.5 including round-trip cost and slippage, and require the target to be at least 3× costs. Use the same stop definition for sizing and for order placement. |
| E2 | **High** (verified) | `agents/scanner.py:182, 196-212, 265-279`; `integrations/groww_client.py:269-274` | **Live "VWAP" is not a session VWAP.** It is computed over a 1000-tick deque, and volume lands only on the first tick after each 60 s REST poll. The result is a weighted average of about 5–15 prices sampled at poll moments over roughly 16 minutes. It returns 0.0 after a restart, and then C1 passes trivially. | C1 (price above VWAP) and every VWAP-deviation gate (`decision_engine.py:353/372, 650-662, 843-909`) are anchored to a number institutions do not defend. The main mean-reversion guard is not doing its job. | Use the broker's session ATP/average price if the quote provides it. Otherwise accumulate `Σprice·vol` and `Σvol` from 1m candles starting at 09:15, outside the deque. Use ticks for LTP only. |
| E3 | **High** (verified) | `integrations/groww_client.py:847-863`; `agents/scanner.py:228-234, 667-668` | **The gap filter compares the open with the seeded LTP, not the previous close.** `get_ohlcv_snapshot` overwrites `close` with `last_price`, even though its own comment says `ohlc.close` is the previous settlement. When the store is seeded near 09:15, the gap reads about 0%, so gap-ups always pass. When it is seeded late or after a restart, the filter instead rejects stocks that have faded more than 1–2% from the open. The backtest uses the true previous close (`runner.py:331-337`). | Live, the system buys extended gap-ups that tend to fade intraday, which is exactly what the filter was written to stop. It also blocks the wrong names, and the backtest hides all of this. | Return `prev_close` as a separate key, or take it from yesterday's daily candle (already fetched in `_load_avg_volumes`). Seed it once before the open, and add a parity test against the backtest. |
| E4 | **High** (verified) | `agents/scanner.py:566, 764-766, 839, 857` | **The NIFTY intraday trend filter never activates.** Nothing ever writes `_ohlc_cache["NIFTY 50"]`, so `_nifty_open_price` stays at 0. The bias-scaled A2 thresholds and the dashboard's NIFTY flag are dead in live trading. The backtest *does* apply the filter (`runner.py:280, 771`). | A long-only momentum book keeps buying high-beta names on a falling index day, which is the "too aggressive in bear markets" failure FINSABER describes. The backtest looks better than live because it filters days that live does not. | Seed the NIFTY open from the index quote or the first index tick at or after 09:15. Add NIFTY-vs-its-VWAP, breadth and intraday VIX change. Fail closed and alert if the regime input is missing in live. |
| E5 | **High** (verified) | `agents/decision_engine.py:267-276`; `integrations/groww_client.py:914-915` | **The daily EMA21 trend filter is dead in live trading.** `days_back=25` counts calendar days, which gives at most about 18–19 bars. `< 21` then fails open, so every symbol is treated as "uptrending". The backtest's 35-day history gives about 24 sessions, so the filter *is* active there. | Live trades downtrend names that the backtest removes. This is another bias that makes the backtest look better than live. | Fetch about 45 calendar days or count trading days. Alert when the filter fails open. Share one implementation between live and backtest. |
| E6 | **Medium** (verified, downgraded from high) | `agents/scanner.py:299-318` | **The volume ratio is cumulative day volume over a linearly prorated 20-day average.** NSE volume is U-shaped through the day, so this inflates readings early (about 1.8–3× at 09:30–09:45 on an ordinary day) and deflates them midday. A heavy open keeps the ratio high for hours. | The "volume surge" gate becomes a time-of-day filter that pushes entries into the noisiest window. It is biased, not meaningless: other AND-ed gates help, and the midday under-reading mostly causes missed trades. | Build a per-symbol minute-of-day volume profile (median over 10–20 sessions) and compare against it. Add a short-window (5–15 min) relative-volume measure. Use the bar timestamp, not the wall clock. |
| E7 | **Medium** (verified, downgraded from high) | `agents/scanner.py:281-297` | **RSI(14) is computed on raw LTP ticks**, so in live trading it covers seconds to about a minute. Other candle-based gates (EMA/MACD, 5m RSI, decision-engine RSI 40–72) cushion the effect. | It adds random admission and rejection to the primary momentum gate. See V3 for how this breaks the backtest. | Compute RSI on completed 1m or 3m closes in both live and backtest. |
| E8 | **Medium** (verified, downgraded from high) | `agents/risk_manager.py:320-341, 370-381` | **The exit machinery is calibrated for 1–2% stops**, but targets are about 0.25–0.45%. The trailing stop activates at +0.8%, which is past the target. ROI decay applies only when the decayed target is below the current one, and with a 0.5% floor that never happens. Both only work in the ATR=0 fallback (before about 9:30) or on unusually volatile names. | In practice, exits are static SL/target, the 1R partial and the 15:00 flatten. The trailing and decay settings give a false impression of sophistication. Partial-at-1R followed by a breakeven stop tends to lock in a small net loss on the runner half after costs. | Express every exit parameter in R or same-timeframe ATR. Add a unit test that the exits are reachable at a typical ATR. A/B test the stacked exits in a backtest that can actually simulate them. |
| E9 | Medium ⚠ not independently verified | `agents/decision_engine.py:468-483` | **Long-only, and BEARISH or AVOID_TRADING is a hard reject** based on a stale pre-market LLM label, with no use of `bias_confidence`. | The system sits idle for long stretches and leaves half the opportunity set unused. | Add a mirrored short setup on liquid F&O names, gated by an intraday regime signal. Block only on BEARISH with confidence ≥ 0.6; otherwise tighten thresholds. |
| E10 | Medium ⚠ | `agents/scanner.py:404-406, 512-517, 699, 378-381` | **Opening-range handling is inconsistent.** Signals are possible from 15 candles, but EMA/MACD only apply from 26 candles, and the 5m HTF RSI is inactive until about 10:30. | Early entries skip filters, and the inflated opening ATR sets the stop. | Use an explicit ORB model, or block entries until 09:45 or later, and backfill HTF candles. |
| E11 | Medium ⚠ | `agents/scanner.py:1049` | **No intraday backfill.** A restart wipes candles, VWAP and indicator warmup for 15–75 minutes. | Wrong indicators right after any crash. That is common on Termux. | Seed the candle builders and session VWAP from today's 1m history on start. |
| E12 | Medium ⚠ | `agents/scanner.py:722` | **The 30-minute cooldown is set when the scanner fires**, even if the decision engine or the LLM then rejects the signal or fails. | Valid setups get locked out for 30 minutes because of a cheap pre-check reject or an LLM error. | Set the full cooldown only on EXECUTE/REDUCE or a stop-out. Move the deterministic gates into the scanner, ahead of the cooldown. |
| E13 | Medium ⚠ | `agents/scanner.py:139-148, 215-223` | **Candles are built from LTP-only ticks with wall-clock timestamps.** High and low are understated, and minutes with no ticks produce no candle. | ATR is biased low, which makes the stop even tighter (it compounds E1). | Use the exchange's 1m OHLCV bars as the source of truth for candles. |
| E14 | Medium ⚠ | `core/config.py:134-136`; `integrations/instrument_service.py:94-159` | **The universe is low-ATR mega-caps**, with no liquidity or volatility screen. FALLBACK_TOKEN_MAP and TRACKED_SYMBOLS disagree on 8 names. Feed subscriptions are fixed at connect time, so a mid-session watchlist change silently drops ticks. | The intraday momentum evidence (e.g. the Zarattini ORB study) relies on stocks in play. Low ATR makes the cost problem in E1 worse. | Each morning, build an F&O-eligible universe filtered by ADV above ₹100 Cr and daily ATR% above 1.5%, then rank by gap and RVOL. Use the LLM as a veto, not the selector. |

### 4.2 Risk and execution

| # | Sev | Where | Finding | Why it costs money | Fix |
|---|---|---|---|---|---|
| R1 | **Critical** (verified by 3 lenses) | `agents/trading_agent.py:348, 418`; `integrations/groww_client.py:535-599`; `agents/risk_manager.py:444-466, 601-677`; `api/routes/trades.py:92` | **The broker SL order ID is always lost.** `place_gtt` returns an `int`, but the caller does `gtt_resp.get(...) if isinstance(gtt_resp, dict) else None`, so `gtt_trigger_id` is always None and the failure is silent. As a result, exits (target, EOD, manual, partial) never cancel the resting SL SELL. Partial booking leaves the original full-size SL live *and* adds a new one, so 1.5× qty sits against 0.5× held. Trailing and breakeven moves never reach the broker. | (a) After a system exit, a pullback to the old stop sells shares you no longer hold, which opens a **naked MIS short**. (b) On a stop hit, the broker SL and the poller's market SELL can both fill, leaving you short by the full quantity. (c) After a partial booking, you are oversold. Exposure lasts until the broker auto-squares around 15:20, with its square-off fee. Paper mode cannot show any of this. | Store the int, or better, the full raw order ID in its own `sl_order_id` column. Have one exit owner per position: either modify the broker SL on every trail, or cancel it **and confirm the cancel** before any SELL. Before every exit, sell `min(db_qty, broker_net_qty)`. Add an integration test of place_gtt → execute → close against a mocked broker. |
| R2 | **Critical** (verified by 3 lenses) | `integrations/groww_client.py:749-764`; `agents/risk_manager.py:173-208, 257-264` | **A failed LTP fetch becomes 0.0, which triggers STOP_LOSS_HIT on every open position.** `get_ltp` swallows every exception and also fills missing or null symbols with 0.0, so the re-raise path in `_build_ltp_map` can never run. There is no `ltp <= 0` guard anywhere. With `paper_risk_use_broker_ltp=True`, paper mode does the same thing. | One Groww blip, a 429, or one symbol missing from the response sends a real MARKET SELL for the whole book. It also books exit_price=0, which is about −100% of notional (around −₹33k per trade), sets 8 h stock locks, adds to the loss streak and trips the 3% HALT. **Paper statistics get polluted with fake catastrophic losses.** | Return None or raise on failure, never 0.0. Skip any trade whose LTP is ≤0, missing, or more than about 5% away from the last ws tick, and alert after N misses. Use the WS `ltp_store` as the primary price, with REST as a cross-check. Add a unit test where get_ltp returns 0.0 and assert that nothing exits. |
| R3 | **Critical** (verified) | `agents/trading_agent.py:280-314, 401-420` | **Entry fills are never confirmed.** The code polls 3 × 0.5 s for `COMPLETE` only. REJECTED, CANCELLED, still-open or partial fills fall through. It still places a full-size SL SELL and saves the trade as OPEN at full qty and `signal.ltp`. `filled_quantity` is returned but never used. | A rejected BUY (margin, price band, freeze qty) leaves a live sell-stop *and* a DB position that the poller will later sell, so you end up with a naked short. A partial fill means every exit oversells. | Wait for a terminal state with a timeout, and cancel any remainder. Persist `filled_quantity` and `average_price`. If nothing filled, write no row and place no SL. |
| R4 | **High** (verified) | `agents/risk_manager.py:455-509, 620-627` | **Exits are marked CLOSED on acknowledgement, not on fill**, using the polled LTP as the exit price. The GTT is cancelled *before* the SELL is confirmed. | A SELL that is accepted and then rejected or left unfilled (lower-circuit lock, RMS reject) leaves a live position with **no broker stop and no monitoring**, plus fictitious P&L feeding the halt and streak logic. Even normal exits ignore slippage in the books. | Poll the exit order to COMPLETE/REJECTED and book from its `average_price` and `filled_qty`. Keep the trade managed until it is confirmed flat. Check `lower_circuit_limit` before relying on a market exit. |
| R5 | **High** (verified, downgraded from critical) | `integrations/groww_client.py:71-100, 505-520, 569-582` | **`_retry_sync` retries non-idempotent orders.** Any exception other than 401/403 is retried, for up to 3 attempts with no idempotency key and no order-book check. | A read timeout after the broker accepts the order produces up to 3 BUYs, or turns an exit into a short. A retried `place_gtt` can leave several SL orders resting. | Never blindly retry order placement. Send a unique `order_reference_id`; on an ambiguous failure, look the order up first. Retry only errors that clearly failed before reaching the exchange, such as connection refused. |
| R6 | **High** (verified) | `agents/risk_manager.py:152-160, 434-439, 483-489`; `api/routes/trades.py:77-89` | **Risk polls can overlap, and the compare-and-swap result is never checked.** A `future.result(timeout=30)` timeout does not cancel the poll, so the next poll starts on top of it. `_close_position` never reads the rowcount from its OPEN→CLOSING update and sells anyway. A manual close racing with a poll gets a second SELL plus overwritten P&L. | Duplicate MARKET SELLs lead to an MIS short. The worst case is the 15:00 square-off with slow broker calls. | Use `.returning(Trade.id)` and abort when no row is returned. Add a single-flight guard on the poll. Make the CLOSED update conditional on `status=='CLOSING'`. Run the loop as an asyncio task rather than a thread. |
| R7 | **High** (verified) | `agents/trading_agent_manager.py:125, 257-263`; `main.py:339-353` | **A restart mid-session clears HALT and the loss-streak pause.** `_restore_trade_count` unconditionally sets HALT=FALSE, `consecutive_losses=0` and an empty pause. This also wipes the manual halt, the GTT-failure halt ("manual intervention required") and the Groww circuit-breaker halt. The drawdown halt comes back only after the *next* position opens, because `_poll` returns early when there are no positions. | A crash or a Termux restart re-arms trading at exactly the moment the kill switch matters. At least one more trade gets through past the 3% limit, and a position with no broker stop can coexist with resumed entries. | Store halts as `halt:{YYYY-MM-DD}` and reset them only on a new trading day. On startup, recompute the streak and drawdown from today's DB rows. Require an authenticated manual resume to clear a halt. |
| R8 | **High** (verified) | `agents/trading_agent.py:56-170` (esp. 126-153) | **State recovery restores a short as a long and never reconciles in reverse.** It applies `abs(qty)` and `direction="BUY"` even when net qty is negative. DB rows that are OPEN while the broker is flat (for example, the exchange SL fired while the process was down) are left as they are. Recovery is skipped in paper mode and on errors. | An accidental short from R1/R5/R6 gets "managed" as a long, and the next SELL deepens it. A stale OPEN row gets sold again, opening a new short. | Use the sign of net qty, or flatten any unknown position and alert. Reconcile both directions every few minutes, not only at startup, and close rows from broker fills. |
| R9 | **High** (verified) | `agents/risk_manager.py:418, 881-906` | **Stuck CLOSING rows are never retried and are booked at entry price with zero P&L.** `_get_open_trades` selects only OPEN rows. At 15:30 the EOD report sets every CLOSING row to CLOSED at entry price with `realized_pnl=0`, without asking the broker. The GTT was already cancelled, and the "target hit" alert has already gone out. | The position runs unprotected until the broker auto-squares it, and the real loss is erased from the drawdown figure and the daily report. | Keep CLOSING rows in the monitored set and retry or confirm against the broker. Wire up the unused `MIS_FORCE_CLOSE` (15:20) for escalation. Reconcile P&L from broker fills. |
| R10 | **High** (verified, downgraded from critical) | `agents/decision_engine.py:815`; `agents/trading_agent.py:320-325`; `agents/risk_manager.py:635, 662`; `integrations/groww_client.py:575-585` | **SL and target prices are not rounded to the NSE tick size.** They are rounded to 2 decimals, and the backend has no tick-size handling anywhere. | ATR-derived prices like 1423.37 get rejected. The `place_gtt` exception path then sets HALT and leaves the position without a broker stop (the client-side poll still runs). **The first live trade will very likely halt the system.** | Load tick size per instrument. Floor the SL and ceil the target to the tick, for every price sent to the broker. Add a unit test with misaligned values. |
| R11 | **High** (verified by LLM lens; medium ⚠ in risk lens; merged) | `schemas/decision.py:20`; `agents/decision_engine.py:793-986`; `integrations/groww_client.py:502`; `agents/risk_manager.py:384` | **The LLM-chosen `product_type` is never enforced.** CNC is a valid enum value in the forced-tool schema, and only one sentence in the prompt forbids it. A CNC answer becomes a DELIVERY buy, which skips the 15:00 MIS close and drops out of monitoring the next day because `_get_open_trades` filters on today's date. Its SL is placed as INTRADAY, which does not match the position. | The chance per call is low, but the result is an unmonitored overnight position in a system built to be flat by end of day. | Force `product_type=MIS` in `_validate_decision`, remove CNC from the schema, and assert it again in `_execute_trade`. |
| R12 | **Medium** (verified, downgraded) | `integrations/groww_client.py:569-582` | **The broker stop is an SL-limit with limit equal to trigger**, despite the comment calling it "SL-M equivalent". It also ignores the caller's MARKET/product arguments, and there is no target leg even though the docstring says "OCO". | On a gap or fast drop through the level, it does not fill. The 5 s poller is the backstop, but it is not there if the process or host is down, which is exactly when a broker stop matters. | Use SL-M if Groww supports it. Otherwise set the limit a buffer below the trigger, tick-aligned. Check Groww CASH OCO support for MIS and attach a target leg if it works. |
| R13 | **Medium** (verified, downgraded) | `agents/scanner.py:455-468`; `agents/decision_engine.py:540` | **Risk-based sizing never applies.** The ₹33k notional cap (capital ÷ 3) always binds before the ₹1,500 risk budget would, so real risk per trade is about ₹60–330 and varies with the stop width. The 3% halt needs 9 or more full stops to trigger. BULL/FULL days allow 5 slots against a ÷3 cap, about 1.67× notional (still well within ~5× MIS leverage). There is no margin check. Correction from the verifier: stacked halvings max out at 1/4, not 1/8. | `max_loss_per_trade_pct` is dead config. The risk unit is inconsistent, which makes R-based statistics meaningless. | Size on risk first, then cap notional by the live available margin. Divide by the position limit actually in force that day. Use one combined size multiplier with a minimum meaningful notional. |
| R14 | **Medium** (verified) | `agents/risk_manager.py:263, 514-528, 540-547` | **Trailing and breakeven exits are labelled STOP_LOSS_HIT**, so profitable exits set the 8 h stock lock, add to the loss streak, and trigger "Loss" alerts. | Good days get suppressed: a winning stock is barred, and three trailed winners trigger the 30-minute pause. Exit-reason analytics are skewed. | Add TRAILING_SL and BREAKEVEN exit reasons. Drive the lock and streak from the sign of net `realized_pnl`. |
| R15 | Medium ⚠ | `agents/risk_manager.py:779-852` | **The drawdown halt blocks entries only.** It never flattens, winners never offset losses, and the published `drawdown_pct` is loss ÷ limit. | A 3% halt still leaves open MIS positions running. | Base it on net daily P&L, flatten on a hard breach, and publish both percentages. |
| R16 | Medium ⚠ | `agents/decision_engine.py:536` | **No sector, correlation or beta limit** on a long-only Nifty book. | Several bank or IT longs during a sell-off break the per-trade risk assumption. | Cap names per sector, beta-weighted notional and total open risk. |
| R17 | Medium ⚠ | `agents/risk_manager.py:478`; `agents/trading_agent.py:239-246, 317` | **P&L is booked at the polled LTP, not the fill.** Paper fills have no slippage, and in paper mode SL and target stay anchored to `signal.ltp` while entry uses the current LTP. The manual close leaves `booked_pnl` out. | Paper P&L is optimistic and its R is skewed, which makes it an unreliable go-live signal. | Book from `average_price`. Add a slippage model and recompute SL/target from the fill in paper mode too. |
| R18 | Low ⚠ | `agents/risk_manager.py:384` | **The 15:00 MIS exit has no retry or escalation**, and a symbol missing from `ltp_map` is skipped. | It falls through to the broker's auto-square-off, with its fee and an uncontrolled price. | Retry every cycle until the broker confirms flat, and escalate at 15:10 without needing an LTP. |

### 4.3 India-specific costs and microstructure

| # | Sev | Where | Finding | Why it costs money | Fix |
|---|---|---|---|---|---|
| I1 | **High** (verified) | `agents/decision_engine.py:851`; `agents/risk_manager.py:52-92, 479, 688`; `api/routes/trades.py:138`; `backtest/runner.py:38, 513` | **Costs are ignored in sizing, the R:R gate and the backtest.** They are applied only to realised P&L. Each full stop therefore loses max_loss plus about ₹35. | At about 0.107% round trip, costs are **35–70% of a 1m-ATR 1R**, so the gross 2:1 becomes roughly 1.2:1 typical and about 1:1 when ATR is tight. Live accounting is correct, but the costs never feed back into the decision. (Same root cause as E1.) | Add a net R:R gate as in E1. Set a minimum ₹ stop distance so costs stay below about 15% of R. Charge costs in the backtest (see V2). |
| I2 | **High** (verified) | — | Tick-size rejection (see **R10**). | — | — |
| I3 | **Medium** (verified, downgraded) | `core/nse_calendar.py:43-53, 70` | **`NSE_HOLIDAYS_2026` is empty** and the lookup silently fails open. Diwali 2025 is wrong (it should be 21 Oct Muhurat and 22 Oct), 2 Oct is listed twice, there is no 2027 entry, and Muhurat sessions are not handled. | On 2026 holidays the system wastes Sonnet and Haiku spend, sends session-start alerts, shows a misleading "Market Live" status, and may act on stale cached LTP. Real fills are unlikely. | Populate the calendar from the NSE circular or fetch the holiday master daily. Alert loudly when the current year is missing. Add expiry, budget-day and RBI-policy-day awareness. |
| I4 | **Medium** (verified, downgraded) | `integrations/groww_client.py:475-532`; `scripts/android` | **No static-IP awareness or startup check** (see §3). | Live orders from Termux are rejected. The failure is loud, but it is a deployment blocker. | Run live only from the Oracle VM with a reserved IP. Add a startup check that compares egress IP with the whitelisted IP and refuses live mode on a mismatch. |
| I5 | **Low** (verified; research said this exceeds limits every minute, verifier refuted that for the default config) | `agents/scanner.py:843, 940`; `integrations/groww_client.py` | **No REST rate limiter.** The per-symbol `get_quote` fan-out runs every 60 s. With the default 6–10 symbols this stays within Groww's 10/s and 300/min, but the 50-symbol fallback map would breach it. Failures are swallowed (`{}`, 0.0, or `avg_vol=0`, which silently disables the volume filter). Blocking `time.sleep` in `to_thread` workers can delay order calls during a failure storm. | Fragile as the universe grows, and it can feed the 0.0-LTP bug in R2. | Use the batch LTP/OHLC endpoints, add a token-bucket limiter, a dedicated executor and async backoff, and treat a 429 as backoff. |
| I6 | Medium ⚠ | `integrations/instrument_service.py:185` | **No series filter (EQ), and no ASM/GSM, T2T/BE or F&O-ban screening**, even though the comment says it filters. | It can trade names that are not MIS-eligible or are under surveillance. | Filter to `series=='EQ'`, check MIS eligibility, and add the daily ASM/GSM and ban lists to `avoid_today`. |
| I7 | Medium ⚠ | `models/trade.py:33` | **Weak audit trail.** Exit and SL order IDs are not stored, the SL ID is truncated to an 8-digit int, and money is stored as Float. | You cannot reconcile against contract notes or for tax. | Store the full order ID for each leg plus fill qty and price, use `Numeric(14,2)`, and produce a daily export. |
| I8 | Low ⚠ | `agents/risk_manager.py:76` | **Cost model details.** It uses the Zerodha-style 0.03% brokerage cap, an outdated exchange rate, no GST on the SEBI fee, and charges entry brokerage twice on partial booking. | Small P&L misstatement. | Move costs to config, check them against a real Groww contract note, and prorate the entry leg. |

### 4.4 LLM usage

| # | Sev | Where | Finding | Why it costs money | Fix |
|---|---|---|---|---|---|
| L1 | **High** | — | CNC product type (see **R11**). | — | — |
| L2 | **Medium** (verified, downgraded) | `agents/decision_engine.py:98-163, 793-982`; `integrations/anthropic_client.py:120-130` | **Haiku mostly rubber-stamps the scanner.** The scanner already guarantees EMA and MACD, so the prompt's +5 bonuses always apply and its penalties never fire. The code overwrites everything except `confidence_score` and REJECT. The decisive penalties (dead zone, late session, opening, bias confidence, earnings) are fixed formulas that an LLM computes **at the default temperature of 1.0**. The LLM does add some qualitative news judgement. | 1–5 s of latency per signal, and random EXECUTE↔REDUCE flips that make paper results irreproducible. Nothing measures whether vetoes improve expectancy. | Move the calibration into Python. Use the LLM only for a news or filing veto at temperature 0. Log its veto next to the deterministic verdict. |
| L3 | **Medium** (verified; low ⚠ in edge lens; merged) | `agents/decision_engine.py:700-713, 727-740`; `schemas/market_brief.py:134` | **The prompt applies VIX-regime and earnings-days penalties that it is never given data for.** It also says 0.8%/1.6% SL/target while the config uses 1%/2%. Upstream gates catch the worst cases: STRESS maps to AVOID_TRADING, and earnings-today names go to `avoid_today`. | The one trusted LLM output, confidence, is miscalibrated at random, which pushes the model toward hallucinated inputs. | Pass the VIX regime and value and the earnings dates, or better, apply these penalties in code. Generate the prompt numbers from settings. |
| L4 | **Medium** (verified, downgraded) | `integrations/anthropic_client.py:122-139, ~205-217`; `agents/research_agent.py:817-827` | **Extended thinking combined with a forced `tool_choice` gets a 400 on every research run.** The handler strips thinking and uses up 1 of the 2 attempts. A single application-level failure on the remaining attempt (ValidationError, missing tool block, truncation) falls back to `_generate_mock_brief`, which still sets the bias and universe. SDK-level retries do cover transport errors. | The reasoning step never runs, every brief wastes a request, and a heuristic brief can silently drive the day. | Drop thinking or use tool_choice `auto`, and don't count the strip toward the retry budget. Persist `brief_source: llm\|heuristic`. |
| L5 | **Medium** (verified, downgraded) | `agents/research_agent.py:338-389, 807-813, 910`; `integrations/news_aggregator.py:241, 292` | **Raw scraped headlines and NSE filing text go straight into the brief prompt**, with no "treat as untrusted" instruction. Watchlist tickers are not checked against an allow-list. | A crafted or fake headline can flip the day to BEARISH or AVOID_TRADING (a fail-safe, opportunity cost) or widen the universe beyond the NIFTY 50. Downstream gates still apply. | Put scraped text in delimited untrusted blocks and cap its length. Intersect the watchlist and avoid list with `core/nifty50.py`. Require a sanity check (GIFT gap, VIX) before AVOID_TRADING. |
| L6 | **Medium** (verified, downgraded) | `agents/trading_agent.py:182-226`; `agents/decision_engine.py:397-682` | **Decisions run serially with no signal-age check.** VWAP and circuit gates use the scan-time price, and the order is MARKET. The worst-case LLM stall is about 185 s or more, though it usually results in a discarded signal. In paper mode, entry uses the current LTP while SL and target use `signal.ltp`. | Entries can chase setups that have already passed, and paper R is distorted. | Drop signals older than 5–10 s, re-read the LTP and re-check VWAP deviation just before the order. Use a short decision timeout and fail closed. Recompute SL/target from the fill in paper mode. |
| L7 | Medium ⚠ | `agents/decision_engine.py:1022` | **LLM failure silently drops the signal** and still burns the 30-minute cooldown. There is no failure-rate circuit breaker and no deterministic fallback, and 400s and over-length rationales use up retries. | Lost trades and no visibility. | Track LLM health and switch to the deterministic path or halt with one alert. Don't retry 400s, and truncate the rationale. |
| L8 | Medium ⚠ | `agents/research_agent.py:807`; `integrations/news_aggregator.py:196` | **The 12:30 refresh re-runs the pre-market prompt on stale overnight data**, can change the bias and universe mid-day while feed subscriptions stay fixed, and undated news is stamped "now". | It changes the regime without intraday evidence. | Give the midday run its own inputs (NIFTY vs open, breadth, VIX change), or limit it to updating `avoid_today`. |
| L9 | Medium ⚠ | `agents/decision_engine.py:472` | **The brief's `date` and `generated_at` come from the LLM** and are used as the DB upsert key. | It can corrupt the brief history. | Set both from the server clock. |
| L10 | Low ⚠ | `api/routes/market_brief.py:53` | **No spend cap, and `/run` is unauthenticated.** | An attacker can burn Sonnet credit. | Require the admin key, rate-limit the endpoint, and enforce a daily dollar cap. |
| L11 | Low ⚠ | `integrations/anthropic_client.py:111` | **Prompt caching is probably inactive for Haiku.** The prefix is about 2.1k tokens, below Haiku 4.5's 4,096 minimum. | You pay for cache writes that are never read. | Check that `cache_read_input_tokens` is above 0, or remove the breakpoints. |

### 4.5 Validation and backtest

| # | Sev | Where | Finding | Why it costs money | Fix |
|---|---|---|---|---|---|
| V1 | **High** (verified) | `backtest/runner.py:62, 69-88, 345-470` | **The backtest is a hand copy of `_check_signal`** with "MUST stay in sync" comments, a fixed NEUTRAL bias and no parity test. It has already drifted: the NIFTY filter (E4), the gap filter (E3) and the daily EMA21 filter (E5) all work in the backtest and are dead in live. | Backtest win rate and profit factor are biased upward in one direction and describe a stricter strategy than the one that trades. | Make the signal a pure function `signal_conditions(store, now, bias, nifty_state, cfg)` that both callers use. Read constants from Settings. Add a golden tick-sequence parity test. Fix E3, E4 and E5 first. |
| V2 | **High** (verified) | `backtest/runner.py:38, 441-446, 475-514` | **No costs, slippage or latency in the backtest.** Entries fill at the signal LTP and stops fill exactly at the SL price. | With costs at 27–53% of 1R, the backtest cannot tell an edge apart from a strategy that loses money after costs. | Charge `_transaction_costs` on every trade. Model slippage (half spread plus k bps, more on stops). Enter at the next bar's open. Fill stops at min(SL, next price). Run a sensitivity table at 0/5/10/20 bps. |
| V3 | **High** (verified) | `backtest/runner.py:295-340`; `agents/scanner.py:182, 265-297` | **Indicators mean different things in replay and in live.** Replay uses 4 synthetic O→L/H→C ticks per candle, so RSI(14) covers about 3.5 minutes and the VWAP window about 250 minutes. Live, RSI covers about 14 seconds and VWAP about 16 minutes of poll samples. | Thresholds tuned in the backtest do not carry over, so live signals are effectively untested. | Compute RSI on 1m closes and VWAP as a session VWAP from candles, in both modes. Alternatively, record live ticks and replay those. |
| V4 | **Medium** (verified, downgraded) | `backtest/runner.py:475-516` | **The backtest skips the decision-engine gates and the live exit logic.** It has binary exits with TIMEOUT at about 15:29 instead of 15:00. It omits the VWAP-deviation, dead-zone, hour≥15 and watchlist rejects, partial booking with breakeven, trailing, ROI decay and portfolio caps, and it accepts signals between 15:00 and 15:14. The docstring admits part of this. | Its payoff profile is all-or-nothing 2R/−1R, while live is path-dependent, so the expectancy it reports does not describe live results. | Build an event-driven simulator on one clock that reuses the RiskManager exit state machine and the deterministic `_pre_check`/`_validate_decision`, with the LLM stubbed. |
| V5 | **Medium** (verified, downgraded) | `backtest/runner.py:665-741` | **Its metrics cannot support a capital decision.** Profit factor excludes TIMEOUT trades (including per phase). P&L is a simple sum. There is no Sharpe/Sortino, max drawdown, R-expectancy or streak statistics, and output goes to stdout only. | A flattering PF, and no way to check results against the 2%/3% daily limits. | Build a ₹ equity curve with production sizing and costs. Report net expectancy (R and ₹), PF over all exits, max DD, worst day, share of days that would hit the 3% halt, and bootstrap CIs. Write results to Parquet. |
| V6 | **Medium** (verified, downgraded) | `backtest/runner.py:71`; `agents/decision_engine.py:155-159, 487`; `agents/scanner.py:489` | **Tuned in-sample with no walk-forward.** RSI floor raised after looking at results, 5m RSI tightened, and backtest PFs (~0.6 / ~0.55) hard-coded into the LLM prompt as facts. | The out-of-sample results will be worse than reported. | Freeze parameters, run walk-forward over 2–3 years including a stress regime, sweep sensitivity ±10–20%, keep an untouched holdout, and compute Deflated Sharpe / PBO. |
| V7 | **Medium** (verified, downgraded) | `models/agent_log.py:17`; `agents/decision_engine.py:~1097-1127` | **LLM decisions are not durably logged.** `AgentLog` is never instantiated. REJECT/REDUCE decisions and their features exist only in a Redis list capped at 100. Approved trades don't store confidence. The brief itself *is* persisted in `market_briefs`. Replaying the LLM on history is invalid anyway because of memorisation and look-ahead. | No evidence the Haiku gate adds value; it may be pure cost and latency. | Persist every signal → pre-check → LLM output (model, prompt hash, confidence) → shadow outcome to Postgres. Run a rules-only vs rules+LLM shadow A/B on post-cutoff paper data. |
| V8 | Medium ⚠ | `agents/trading_agent.py:239`; `PREPRODUCTION_CHECKLIST.md:79` | **Paper trading is optimistic and skips the broker paths** (no SL order, circuit check or fill polling). Mock ticks have a volume ratio of about 60, no NIFTY data and a gap of about 0. | It cannot reveal R1, R3, R4 or R10. | Refuse paper mode on mock ticks, add slippage, then run live at qty=1 before scaling. |
| V9 | Medium ⚠ | `PREPRODUCTION_CHECKLIST.md:13, 47, 55` | **The checklist has false claims** (Stooq VIX, the RSS feed list, "Groww paper OCO", "drawdown halt complete") and **no quantitative go/no-go gate**. | It signs off a system that is not ready. | Add measurable gates (see §5) and restart and outage drills. |
| V10 | Medium ⚠ | `backtest/runner.py:868` | **Survivorship and selection bias.** Symbols are chosen by hand from today's large caps, with no point-in-time constituents, and the LLM universe cannot be reproduced. | Inflated results. | Use point-in-time Nifty membership and a deterministic universe proxy. |
| V11 | Medium ⚠ | `backtest/runner.py:329`; `agents/scanner.py:205-206` | **Data handling.** The 09:15 candle's volume never enters VWAP. The NIFTY fetch fails silently. Dropped candles are not counted. Signal time is the candle open, while entry uses the close. | Subtle bias. | Seed the cumulative volume properly, fail loudly when NIFTY data is missing, and label signals at the candle close. |
| V12 | Low ⚠ | `backtest/runner.py:603, 828-837` | **O(N²) NIFTY alignment, repeated frame scans and sequential fetches.** | Slow research iteration. Part B covers this in detail. | Use `searchsorted`, a groupby-once dict and a Parquet cache. |

**Refuted and excluded.** No trader-lens item was refuted outright. The overstatements the verifiers corrected (₹25 cost figure, "1/8 size stacking", "throttle and algo tagging are regulatory gaps", "rate limits exceeded every minute", "drawdown halt permanently undone", "watchlist cannot be measured forward") have been dropped or corrected above.

---

## 5. Top 10 actions before risking real capital

1. **Fix the broker stop lifecycle (R1, R10, R12).** Store the full SL order ID, give each position one exit owner, and cancel-and-confirm (or modify) the SL before any SELL or trail. Round to the tick size. Use SL-M or a buffered SL. Check whether Groww CASH-MIS OCO actually works, and use it if so. *This is the biggest live-money risk in the codebase.*
2. **Make prices fail safe (R2).** `get_ltp` must never return 0.0. Discard LTPs that are ≤0, missing or implausible, prefer the WS store, and add a unit test where get_ltp returns 0.0 and nothing exits.
3. **Build a real order lifecycle (R3, R4, R5, R6).** Confirm fills on both entry and exit and act on `filled_quantity` and `average_price`. Never blindly retry orders; send `order_reference_id`. Check the CAS rowcount, add a single-flight guard on the risk poll, and keep the trade managed until the broker confirms it is flat.
4. **Reconcile with the broker continuously (R8, R9, R18).** Run a two-way position and order reconciliation every few minutes and on startup. Use the sign of net qty. Retry or escalate CLOSING rows, wire up `MIS_FORCE_CLOSE`, and never book a trade at entry price with zero P&L.
5. **Build a kill switch that survives restarts (R7, R15).** Store halts by date, rebuild the streak and drawdown from the DB on start, require an authenticated manual resume, and on a hard breach flatten everything and cancel all pending orders.
6. **Close the cheap hard gaps (R11, I3, I4, §3 auth).** Force MIS. Populate the 2026 calendar and alert when a year is missing. Run live only from the static-IP VM, with an egress-IP check at startup. Put auth and TLS on every order-placing or LLM-spending route.
7. **Fix the live data so the filters do what their names say (E2, E3, E4, E5, E7).** Session VWAP from candles or ATP, the real previous close, a populated NIFTY open, an EMA21 lookback that returns at least 21 bars, and RSI on candle closes. Add an intraday regime input that fails closed.
8. **Re-price the trade (E1, I1, R13, E8).** Base the stop on a higher-timeframe ATR with a floor. Gate on net-of-cost R:R ≥ 1.5. Size from risk with a margin-aware notional cap. Express trailing and decay settings in R so they can actually fire. Consider a stocks-in-play universe with higher ATR (E14).
9. **One code path and an honest backtest (V1–V6).** Share the signal and exit modules between live and backtest. Model costs, slippage and next-bar entry. Use a portfolio clock with caps and a 15:00 flatten. Run walk-forward over 2+ years with a holdout, and report net R-expectancy, max DD and bootstrap CIs. **Go/no-go: out-of-sample net expectancy above 0, with a CI that excludes 0.**
10. **Prove or demote the LLM (L2, L3, L4, L6, V7).** Log every decision durably. Run a rules-only vs rules+LLM shadow A/B on post-cutoff paper data from the **real feed**, with at least 100 trades. Set temperature 0, move the formula penalties into code, fix the thinking+tool 400, and add signal-age and price re-checks before ordering. After that, go live at **qty=1** for a reconciliation drill: the exit cancels the SL, positions are flat at EOD with no pending orders, plus kill-process and REST-outage tests. Scale up only once those pass.

*Optional early safety ramp, not a substitute for the items above: a Telegram confirm/skip step for live entries in the first weeks. Telegram is already integrated.*


---

# PART B: Architecture & Code Review

*Written from the seat of a veteran tech architect. Scope: `C:\repos\Personal\Projects\AutoTrader\trading-system` (FastAPI backend, Postgres/Redis, Angular 17 dashboard, Docker/Oracle VM and Android/Termux deploy paths). This was a read-only review. Line numbers are approximate to ±3 lines.*

---

## 1. Architecture overview and verdict

### 1.1 How the system is put together

```
                06:00 / 12:30 cron                      09:15 cron → TradingAgentManager.start_session
 ResearchAgent (Sonnet 4.6) ──► Postgres market_briefs ─┐
   18 sources (NSE, RBI, RSS,     Redis latest_brief,   │   TradingAgent.start()
   Yahoo/AV)                      today_watchlist ──────┼──► state_recovery (live only, once)
                                  → instrument map      │   ├─ Scanner (GrowwFeed thread | MockTickGenerator)
                                                        │   │    _on_ticks → TickDataStore → _check_signal (13 gates)
                                                        │   │    + 60 s OHLCV REST poll (module-global cache)
                                                        │   │         │ asyncio.Queue(100)
                                                        │   ├─ _process_decisions → DecisionEngine
                                                        │   │    _pre_check (Redis+DB+HTTP) → Haiku 4.5 tool call → _validate_decision
                                                        │   │         │ → _execute_trade: BUY → poll fill → SL order → INSERT Trade
                                                        │   └─ RiskManager (daemon thread, 5 s, run_coroutine_threadsafe → main loop)
                                                        │        SL / partial / ROI-decay / target / trailing / 15:00 MIS / drawdown / EOD
 FastAPI routes + /ws/live (Redis pub/sub relay + 2 s LTP snapshots) ◄── Angular dashboard
```

It runs as a single process, a single uvicorn worker and a single asyncio loop. Several daemon threads sit beside the loop: the GrowwFeed `connect` thread, the RiskManager poller, and the `to_thread` SDK calls that share the default executor. Module-level singletons are everywhere, and they are the reason `--workers 1` is mandatory.

### 1.2 What is genuinely good

- **The LLM is fenced in.** `_validate_decision` recomputes the SL and target, clamps quantity to the scanner's suggestion, rebuilds every audit fact from the signal, and trusts only `confidence_score` and `rationale` from the model. Most hobby LLM traders get this wrong.
- **Deterministic pre-checks run before the paid call.** Halt, lock, streak, caps, dead zone and VWAP/volume bounds are all checked first, so cost and blast radius stay bounded.
- **The defence-in-depth intent is visible**: stock lock after an SL, a consecutive-loss pause, soft and hard drawdown limits, a broker-side SL order, partial booking to breakeven, and a circuit breaker on broker failures.
- **The manual-close route does its compare-and-set (CAS) correctly** (`.returning(Trade.id)` → 409). The pattern is understood, just not applied everywhere.
- **Sessions are well handled**: short-lived `get_db_context`, commit on exit, `expire_on_commit=False`.
- **The frontend is in decent shape**: OnPush, `takeUntil` everywhere, the alert buffer is capped, and the websocket reconnects with backoff.
- **The paper-mode default is safe**: `paper_trading=True`, so none of the order-path bugs below have cost money yet.

### 1.3 Verdict

**The design is sound. The order lifecycle is not production-safe.** The pipeline decomposition is right, but the stage boundaries exist only as names. Every agent talks directly to Postgres, to Redis with literal keys, and to the Groww SDK. The broker client uses *sentinel values* (`0.0` LTP, `{}` snapshot, `int` vs `dict` IDs) as its error contract, while its callers were written against a contract that raises.

Nearly every critical defect comes from three root causes:

1. **No typed broker port.** Errors become sentinel values, and return types mismatch between client and caller.
2. **No single owner of position state.** Close logic is split between the RiskManager and the API, the CAS result is ignored, and nothing ever reconciles against the broker.
3. **Risk state lives in fail-open Redis and is reset on every session start.**

**Recommendation:** do not flip `paper_trading=False` until Phase 0 of the roadmap (§3) is complete. On today's code, one ordinary stop-loss hit in live mode can produce an unintended MIS short.

---

## 2. Findings

**Legend**
- **Verified**: confirmed (or partially confirmed, with corrections applied) by an independent code re-read.
- **Not independently verified**: reported by a single reviewer pass. Plausible and cited, but not re-checked.

Duplicates reported by several reviewers are merged. Claims that were refuted have been removed or corrected inline.

### 2.1 Correctness and concurrency

#### C1. [CRITICAL] The broker SL order ID is always lost, so stale SL orders are never cancelled. Verified.
- **Where:** `backend/agents/trading_agent.py:348`, `backend/integrations/groww_client.py:535-599`, `backend/agents/risk_manager.py:444, 608, 662`, `backend/api/routes/trades.py:92`
- **Defect:** `place_gtt` is typed `-> int`. It returns an 8-digit synthetic ID, and the raw order ID is kept in Redis under `groww_oco:{id}`. The caller does `gtt_resp.get("trigger_id") if isinstance(gtt_resp, dict) else None`, so the DB always stores `gtt_trigger_id=None` in live mode. Every cancel path (`_close_position`, `_book_partial`, manual close) is guarded by `if gtt_trigger_id`, so none of them ever cancels. `_book_partial` uses the same API correctly (`int(placed)`, l.677), which confirms this is a mismatch at one call site.
- **Failure scenario:**
  - (a) The RiskManager or the user exits via target, trailing, ROI decay, EOD or manual close. A market SELL goes out while the full-size resting SL-limit SELL (INTRADAY) is still live. A later pullback triggers it, which opens a full-size MIS short.
  - (b) On a normal SL hit, the broker SL and the RiskManager's own market SELL both fire, giving an immediate short.
  - (c) Partial booking leaves the original full-size SL in place and adds a remainder SL, so the broker holds about 1.5x the position in SL orders.
- **Mitigation, not a fix:** the broker's intraday auto square-off at about 15:20 caps the exposure to one session.
- **Fix:**
  - Return a typed `StopOrderRef(broker_order_id: str)` and persist the raw ID in a new `broker_sl_order_id VARCHAR` column. Drop the truncated int and the Redis indirection.
  - Before any exit SELL: cancel the SL, confirm it is `CANCELLED`, and if it shows `EXECUTED`, skip the SELL and book from the broker fill.
  - Add a live-mode contract test that asserts the ID is persisted.

#### C2. [CRITICAL] A failed LTP fetch returns `0.0`, which triggers STOP_LOSS_HIT on every open position. Verified.
- **Where:** `backend/integrations/groww_client.py:749-764`, `backend/agents/risk_manager.py:199-203, 213-225, 263`, `backend/api/routes/trades.py:104-107`
- **Defect:** `get_ltp` wraps the retried fetch in `try/except Exception` and returns `{instr: {"last_price": 0.0}}`. It also fills symbols missing from the response with `0.0`, and turns null prices into `0.0` via `float(price or 0)`. `_build_ltp_map` only rejects `None`. Its live-mode re-raise branch is dead code because `get_ltp` never raises. `_poll` then evaluates `0.0 <= stop_loss_price` → `_close_position(trade, 0.0, "STOP_LOSS_HIT")`. `_get_trade_ltp` has the same flaw.
- **Failure scenario:** a single 429, timeout or symbol-key mismatch market-sells *every* open position. Each is booked at `exit_price=0`, so realized P&L is about −notional. That sets `stock_lock`, adds to `consecutive_losses` (three positions hit the pause in one cycle), and can trip the drawdown HALT on fictitious losses. Paper mode is also affected because `paper_risk_use_broker_ltp=True` by default, which bypasses the ltp_store fallback. The manual-close route records `exit_price=0` in live mode.
- **Correction:** an expired token that fails inside `get_groww()` *does* propagate, because it is outside the try block. A token that the server rejects during the REST call does not.
- **Fix:**
  - `get_ltp` returns `dict[str, Quote | None]` or raises `QuoteUnavailable`, and never returns `0.0`.
  - The RiskManager skips any trade whose quote is missing, `<= 0`, or stale, and alerts after N consecutive misses.
  - Add a sanity band: reject an LTP more than X% away from the last good tick in `ltp_store`.
  - Add a unit test where `get_ltp` raises and the test asserts no close happens.

#### C3. [HIGH, critical in live] Order placement is retried on any exception without an idempotency key. Verified.
- **Where:** `backend/integrations/groww_client.py:71-100` (`_retry_sync`), `:505-520` (`place_order`), `:568-582` (`_place_oco`). Callers: `trading_agent.py:280`, `risk_manager.py:460, 621`, `api/routes/trades.py:120`.
- **Defect:** `_retry_sync` retries every exception except 401/403 up to 3 times. It sleeps `2**attempt` after *each* failure, including the last, so 2+4+8 = 14 s of blocking `time.sleep` happens inside a worker thread. This resolves a reviewer disagreement: I re-read l.76-99, and the sleep at l.97 runs before the loop exits. The `source` tag is a constant (`"autotrader"`), and no `order_reference_id` exists anywhere in the codebase.
- **Failure scenario:** the broker accepts a MARKET BUY, but the response times out or the connection resets. The order is resent up to 2 more times, giving up to 3x the planned risk (about ₹4,500 instead of ₹1,500). The DB records one quantity and one SL covers it. Exit SELLs duplicate the same way and open a short. The RiskManager's deliberate "leave CLOSING because the sell may have gone out" handler (risk_manager.py:466-473) comes too late, because the inner wrapper has already resent.
- **Fix:**
  - Never auto-retry non-idempotent writes.
  - Send a deterministic per-intent reference (e.g. `{trade_date}-{stock}-{signal_seq}-{leg}`).
  - On an ambiguous error, query `get_order_details` or the order book by that reference before resubmitting.
  - Keep `_retry_sync` for reads only, and convert it to async backoff (see P2).

#### C4. [HIGH] An unchecked CAS plus overlapping polls can sell the same position twice. Verified, with corrections.
- **Where:** `backend/agents/risk_manager.py:152-160` (`_run_loop`), `:433-439` (CAS), `:482-494` (Phase 3), `:694, 744-764` (`_book_partial`, `_update_stop_loss`, `_update_target`)
- **Defect:** `_close_position` runs `UPDATE trades SET status='CLOSING' WHERE id=:id AND status='OPEN'`, ignores the rowcount, then cancels the GTT and sends a MARKET SELL regardless. Phase 3 writes `CLOSED`, `exit_price`, `exit_reason` and `realized_pnl` with only `id=` in the WHERE clause, so it overwrites any other closer's record. The `trade` objects are snapshots taken at the start of the poll, and several broker round trips separate that read from the CAS. Separately, `future.result(timeout=30)` does not cancel the coroutine on timeout, so a slow poll overlaps the next one.
- **Failure scenarios:**
  1. *Most likely:* the operator clicks Close in the dashboard while a poll holds the stale row. The API wins its (correct) CAS and sells. The RiskManager reaches SL, target or the 15:00 EOD branch, its CAS matches 0 rows, and it sells again. Phase 3 then rewrites `MANUAL` as `STOP_LOSS_HIT` and can wrongly set the stock lock and advance the loss streak.
  2. A poll runs longer than 30 s (`get_ltp` retries + `place_order` retries + `delete_gtt`) and overlaps the next poll.
- **Corrections:**
  - The manual-close route *is* properly guarded (refuting one reviewer).
  - The claim that "partial booking races close" is refuted: `_book_partial` runs sequentially in the same loop.
- **Fix:**
  - Use `.returning(Trade.id)` and return immediately if no row comes back.
  - Guard Phase 3 with `status=='CLOSING'`.
  - Add `status=='OPEN'` to the partial, SL and target updates, and use `booked_pnl = Trade.booked_pnl + :leg` in SQL.
  - Make `_poll` non-reentrant: use an `asyncio.Lock`, skip the cycle if it is held, and call `future.cancel()` on timeout.
  - Long-term: one `PositionService.close()` used by both callers (see §3).

#### C5. [HIGH] A rejected or partially filled BUY is recorded as OPEN and gets an SL order. Verified.
- **Where:** `backend/agents/trading_agent.py:280-312, 333, 401-419`. `groww_client.py:520-527` (can return `order_id=""` without raising), `:704` (`filled_quantity` is returned but never read).
- **Defect:** the fill loop polls 3 × 0.5 s and accepts only `COMPLETE`. Any other status (REJECTED, CANCELLED, OPEN, partial) or a timeout falls through to `fill_price = signal.ltp`. An SL SELL for `adjusted_qty` is then placed and an OPEN row is inserted.
- **Failure scenario:** an RMS, margin or circuit rejection leaves a resting SL SELL with no holding, which becomes a short once triggered. The phantom position later gets EOD and target SELLs, each of which opens a real short. On a partial fill, the SL and exit quantities exceed the holding.
- **Fix:**
  - Poll to a terminal status with a timeout.
  - Act only on `COMPLETE`, using the broker's `filled_quantity`.
  - On REJECTED or CANCELLED: alert, persist nothing, and place no SL.
  - On an unknown status: cancel the order, reconcile against `get_positions`, then decide.

#### C6. [HIGH] SL and target prices are rounded to ₹0.01, not to the exchange tick size. Verified.
- **Where:** `backend/agents/decision_engine.py:346-350, 815-823`, `trading_agent.py:136-137, 320-324, 394`, `risk_manager.py:326-330, 374, 596-599`, `groww_client.py:574-575`
- **Defect:** the backend has no tick-size handling at all. `round(x, 2)` values, and the raw average fill price used as the breakeven SL, go straight into `trigger_price`/`price` of an SL order.
- **Failure scenario:** NSE rejects prices that are not tick multiples (₹0.05 historically, price-banded since 2025, e.g. ₹0.10 for ₹1,000-5,000). The first live SL placement raises, `trading_agent.py:351-375` sets HALT, and the position has no broker stop. Breakeven re-placement after a partial booking fails the same way.
- **Fix:** load `tick_size` from the instrument CSV. Snap the trigger *down* to the tick, and set the limit a few ticks below the trigger (or use SL-M if Groww supports it). Use `Decimal` for all order prices.

#### C7. [HIGH] There is no broker reconciliation, and state recovery mis-models shorts. Verified, re-scoped.
- **Where:** `backend/agents/trading_agent.py:56-170` (runs once, from `start()` at l.473), l.85, 105, 111, 134, 142. `risk_manager.py:877-906` (EOD CLOSING reconcile).
- **Defect:**
  - Recovery runs only at startup and only in one direction (broker has a position, DB does not). It takes `abs(qty)` and hardcodes `direction="BUY"` with long SL and target formulas.
  - The RiskManager never reads `direction` and always SELLs.
  - DB-OPEN/broker-flat positions are never reconciled.
  - CLOSING rows count as matched.
  - EOD force-closes CLOSING rows at entry price with 0 P&L and never asks the broker.
- **Failure scenarios:**
  - (a) Normal operation, no crash needed: the broker SL fires before the 5 s poll. The DB stays OPEN, and the RiskManager's SL, target or EOD exit opens a fresh short.
  - (b) After a restart, a net-short orphan (itself a product of C1 or C3) is recovered as a long, and every exit doubles the short.
  - (c) A SELL that actually failed is booked CLOSED at entry price while the shares are still live.
- **Fix:**
  - Run a periodic reconciler (every 30-60 s plus on startup) that diffs broker net positions and open orders against the DB in both directions.
  - Refuse to auto-manage net shorts in a long-only system: alert and halt instead.
  - Mark DB rows that are flat at the broker as `CLOSED/RECONCILED` using trade-book prices.
  - Never size an exit from the DB without checking the broker's net quantity first.

#### C8. [HIGH] A mid-day restart clears the halts and loss counters, and the risk gates fail open on Redis errors. Verified, with corrections.
- **Where:** `backend/agents/trading_agent_manager.py:125, 257-264`, `main.py:339-353`, `core/redis_client.py:121-141`, `decision_engine.py:433-445`, `trading_agent.py:210`
- **Defect:**
  - `_restore_trade_count` runs on *every* `start_session`: startup catch-up, a manual dashboard start, or a dev `--reload`. It unconditionally writes `HALT=FALSE`, `consecutive_losses=0` and `consecutive_loss_pause_until=""`.
  - `get_value` returns `None` on any Redis error, and every gate blocks only on `== "TRUE"`. `set_value` swallows errors, so a HALT write can vanish silently.
- **Failure scenario:** a crash, OOM or redeploy at 11:00 silently re-enables trading after the halt raised when a position had no stop ("manual intervention required"). The same happens after a manual halt or a broker-outage halt, and it ends the loss pause. None of these are ever re-derived. Stock locks are never rebuilt from the DB either, so the bot can re-enter a stock it was stopped out of today. During a Redis outage, halt, lock, pause and the trade-count cap all read as "not set".
- **Correction:** the 3% *drawdown* halt self-heals, because `_check_daily_drawdown` rebuilds it from the DB on each poll. But `_poll` returns early with no open positions, so at least one new trade can be placed before the halt reasserts.
- **Fix:**
  - Persist risk state in Postgres (a `daily_risk_state` row: halt, halt_reason, halted_at, loss_streak, pause_until, locks), with Redis as a cache.
  - Reset only when `trade_date` changes. Never reset on `source='startup'`.
  - Rebuild the streak and locks from today's trades on start.
  - Add `get_value_strict()` for safety gates, so an unreadable state counts as HALTED.

#### C9. [HIGH] The Trade row is written only after the broker order and SL, and a DB failure kills the whole agent. Verified.
- **Where:** `backend/agents/trading_agent.py:280-379` (try block), `:401-420` (insert outside it), `:187-224` (`_process_decisions`, no handler around `_execute_trade`), `:504-535`
- **Failure scenario:** a transient Postgres error after the fill leaves a live position with no row, so there is no RiskManager management of it, and the trade count is not incremented. The exception then propagates and ends `decision_processor`. `asyncio.wait(FIRST_EXCEPTION)` cancels the scanner, and the `finally` block stops the RiskManager, which leaves *every other* open position unmanaged until a manual restart. Cancelling the task at 15:30 can also hit the window between the order and the insert.
- **Fix:** use an outbox / pending-intent pattern: insert a `PENDING` row with the client order ref before sending, then update it to OPEN with the fill. Shield `_execute_trade` from cancellation once the order is in flight. Wrap per-signal processing in its own try/except so one bad signal cannot kill the loop. The reconciler from C7 picks up stranded `PENDING` rows.

#### C10. [HIGH] A scanner crash tears down the RiskManager, and nothing restarts or escalates. Verified, with corrections.
- **Where:** `backend/agents/trading_agent.py:505-535`, `trading_agent_manager.py:201-226`, `scanner.py:992, 1092, 1115-1211`
- **Defect:** a scanner exception (reauth failure, more than 20 transient attempts, or an unclassified error) ends `asyncio.wait`, and `finally` stops the RiskManager. Nothing restarts the session; only the 09:15 cron does. Alerts go to Redis `system_alerts` → dashboard only, and are not wired to Telegram.
- **Correction (refuted part):** a *graceful* feed close does **not** stop the RiskManager, because `FIRST_EXCEPTION` does not fire on a normal return. It instead creates a silent zombie session: no ticks, no signals, status still ACTIVE, and no alert. That is a medium-severity supervision gap.
- **Fix:** run the RiskManager independently of the scanner lifecycle. Supervise the scanner with restart and backoff. Treat a normal return during market hours as a failure. Separate the "stop requested" flag from the "feed closed" flag. Send a Telegram alert whenever any component exits.

#### Not independently verified
- **[MEDIUM]** Scanner state is mutated on the feed thread while the loop reads it (`scanner.py:747-821`). `_stores`, `_signal_cooldown`, the `_ohlc_cache` global and the TickDataStore deques are written from the GrowwFeed thread and read on the loop (OHLCV poll, `/api/agent/scanner-debug`, `set_market_bias`). The `except asyncio.QueueFull` around `call_soon_threadsafe(put_nowait)` (l.813-820) can never fire, because the put runs later on the loop. **Fix:** marshal raw ticks onto the loop (or onto one owning worker), and wrap the put in a function that catches QueueFull and sets the cooldown only after a successful enqueue.
- **[MEDIUM]** The EOD report races the 15:30 stop job (`risk_manager.py:405`, `main.py:283-289`). The report fires inside `_poll` at ≥15:30, which is the moment the stop job joins the thread. **Fix:** run it as its own 15:35 cron job, or `await _generate_eod_report()` inside `stop_session`.
- **[MEDIUM]** Manual close ignores `booked_pnl` (`api/routes/trades.py:136`), so P&L is wrong after a partial booking. **Fix:** use the shared close service.
- **[MEDIUM]** Trailing, ROI-decay and breakeven stop moves update only the DB (`risk_manager.py:372-381, 744, 762`). The broker SL keeps the original trigger, so there are two sources of truth for the stop. **Fix:** `BrokerPort.modify_stop()` on every ratchet, with the DB as a cache of broker state.
- **[MEDIUM]** The broker SL is an SL-limit with limit = trigger (`groww_client.py:579`), so a gap through the trigger leaves it unfilled. **Fix:** use SL-M, or set the limit a few ticks below the trigger.
- **[LOW]** Naive and wall-clock datetimes: `telegram_client.py:120` and `api/routes/pnl.py:61` use `date.today()`/`datetime.now()` while the container runs in UTC. `NSE_HOLIDAYS_2026` is an empty frozenset (`core/nse_calendar.py:53`), so the holiday guard does nothing this year. **Fix:** a single `Clock.now_ist()`, and fail closed when the current year's holiday list is empty.

### 2.2 Performance and complexity

| # | Hotspot | Current | Proposed |
|---|---|---|---|
| P1 | Per-tick indicator pipeline | O(W + k·C) per tick, W=1000 ticks, C≤200 candles, about 8 EWM passes; O(W) dicts per symbol | O(1) per tick; O(1) per candle close; O(1) scalars per symbol |
| P2 | OHLCV poll fan-out | N=50 thread tasks every 60 s, each holding a worker up to about 14 s on failure | ⌈N/batch⌉ calls on a bounded pool with async backoff |
| P3 | SL detection | 5 s REST poll plus round trip plus retries | O(1) per tick from push LTP, with REST kept as a reconciler |
| P4 | Redis access | 2 round trips per op (PING + op); 5-6 sequential round trips on hot paths; 20 GETs for status | 1 round trip per op; pipelines; one MGET |
| P5 | WS broadcast | Sequential awaits; full O(S) snapshot every 2 s | Concurrent with timeout; O(changed) deltas |
| P6 | News dedup | O(n²·L²) SequenceMatcher over about 200 uncapped items | Close to O(n) with token signatures or LSH, or rapidfuzz |
| P7 | Backtest | O(N·M) per-candle masks, per-date filters, iterrows | `merge_asof` (O(N+M)), `groupby`, `itertuples` |

#### P1. [MEDIUM] Full-window recompute on every tick, and the "VWAP" is not session VWAP. Verified.
- **Where:** `backend/agents/scanner.py:236-263` (`add_tick`, `_df`), `:542-545, 677-680, 713` (`_check_signal`), `:265-390` (indicators)
- **Defect:**
  - Each tick dirties the cached DataFrame, so once the cheap gates pass, `pd.DataFrame(self.ticks)` is rebuilt over up to 1000 dicts.
  - VWAP (cumsum), tick RSI (diff + 2 EWMs), and EMA9/21, MACD (3 EWMs), ATR and 5m RSI are all recomputed over full windows, even though the candle-based values change once per minute.
  - In mock mode, all 50 symbols are evaluated synchronously on the event loop every second. In live mode the work runs on the feed callback thread and competes for the GIL.
  - **Semantic bug:** VWAP is computed over the last 1000 ticks (about 16 minutes), not the session, even though the docstring says "intraday VWAP".
- **Correction:** the DataFrame is built *once* per tick (ltp, vwap and rsi share it), and `set_ltp` runs before the indicator work, so the risk LTP for the current tick is not delayed. The realistic load is about 50 updates/s, roughly 10-25% of a desktop core. The cost matters on Termux. "Dropped NATS messages" is unproven.
- **Fix:**
  - Per-symbol incremental state: running `Σpv`/`Σv` for session VWAP, Wilder `avg_gain`/`avg_loss` for tick RSI, and `ltp = ticks[-1]`.
  - Update EMA, MACD, ATR and 5m RSI inside `CandleBuilder` on candle close and cache the scalars.
  - Store only (ts, price, vol_delta).
  - Keep pandas for the backtest only.
  - Net effect: per tick O(W + k·C) → O(1). Memory goes from about 20-30 MB of dicts to a few KB.

#### P2. [MEDIUM] The shared default ThreadPoolExecutor is starved by the feed, the fan-out and blocking retry sleeps. Verified, severity reduced.
- **Where:** `scanner.py:839-845` (gather over all symbols), `:1090` (`run_in_executor(None, ticker.connect)` holds one worker for the whole session), `groww_client.py:71-100` (`time.sleep` inside a worker), `:523, 585, 626, 751` (orders, cancels and LTP on the same pool)
- **Failure scenario:** under Groww rate limiting, 50 symbols × about 14 s of sleep spread across about 11 free workers (phone or small VM) saturates the pool for about 60 s. The RiskManager's `get_ltp`, exit SELLs, and new entries queue behind it, and polls can exceed the 30 s timeout, which feeds C4. The 50-way burst every minute is itself likely to trigger 429s.
- **Correction:** the broker-resident SL (which is still resting because of C1) bounds the hard-stop exposure. What gets delayed is app-driven exits, trailing, EOD square-off and entries.
- **Fix:**
  - Use a batched quote call (the tuple form already used by `get_ltp`).
  - Give the GrowwFeed a dedicated `threading.Thread`.
  - Use separate small executors: one for orders and cancels, one bounded data executor plus a `Semaphore` for market data.
  - Replace in-thread `time.sleep` with `await asyncio.sleep` around single-attempt `to_thread` calls.

#### P3. [HIGH, folded into C4] SL detection polls REST every 5 s while push LTPs sit unused in `ltp_store`. Verified.
- **Where:** `risk_manager.py:173-208`
- **Note:** this is one batched REST call per cycle, not O(positions) calls. The issue is latency (5 s plus round trip plus up to 14 s of retries) and the dependence on C2's sentinel behaviour.
- **Fix:** keep an in-memory `symbol → (sl, target, trade_id)` index for open trades and check it on each tick, or in a fast async task reading `ltp_store`. Keep the REST poll as a 15-30 s reconciler.

#### Not independently verified
- **[MEDIUM]** `get_redis()` PINGs on every call (`core/redis_client.py:40-44`), doubling round trips. `_push_feed_entry` takes 5 sequential round trips. `system.py:104-105` makes 20 GETs and parses the whole brief to read one field. **Fix:** use `health_check_interval` and `retry_on_error`, pipelines, MGET, and a dedicated `recommended_stance` key.
- **[MEDIUM]** The publish fallback buffer is unbounded (`redis_client.py:93`) and replays stale events. **Fix:** a per-channel `deque(maxlen=100)`, last-value-only for idempotent channels, and drop entries older than a few minutes.
- **[MEDIUM]** The module-global `_ohlc_cache`/`_ohlc_last_ts` are never cleared (`scanner.py:49`). A previous session's data can seed today's `_open_price`/`_prev_close`, and the poll task races the first ticks. Note also that the snapshot's `close` is actually the LTP (`groww_client.py:849, 862`), so the gap filter compares open against an early LTP, not the previous close. **Fix:** make the cache an instance attribute owned by the session, date-stamp entries, await the first poll before connecting the feed, and fetch the true previous close.
- **[MEDIUM]** WebSocket broadcast awaits each client in turn (`api/websocket.py:32-42`) and sends a full LTP snapshot every 2 s. **Fix:** `gather` with `wait_for` per client, or per-client bounded queues; send deltas using a version counter in `ltp_store`.
- **[MEDIUM]** The LLM layer wastes an attempt and probably gets no Haiku cache hits (`integrations/anthropic_client.py:126-141, 196-209`). Research sends `thinking` together with a forced `tool_choice`, which is likely a 400 on every attempt 1, burning half of `MAX_LLM_RETRIES=2`. The decision prefix (about 2.1k tokens) is probably under Haiku 4.5's minimum cacheable size; check the existing `cache_read` logs. The schema is rebuilt on every call. **Fix:** `tool_choice=auto` when thinking is on (or drop thinking), don't count capability 400s against the retry budget, move stable context into the cached prefix or drop `cache_control`, and memoize the tool schema.
- **[LOW]** The OHLCV poll task is fire-and-forget (`scanner.py:1055`): no reference is kept, it is never cancelled, and its symbol list is frozen.
- **[LOW]** Each risk poll opens 3 DB sessions (`_get_open_trades` twice plus the losses query, `risk_manager.py:801-813`), and `_pre_check` makes 3 count queries per signal. **Fix:** pass `open_trades` through, use one `count(*) FILTER (...)` query, and add a `(trade_date, status)` index.
- **[LOW]** News dedup runs O(n²) before the cap is applied, on the event loop (`news_aggregator.py:312, 389-396`). The docstring wrongly assumes n ≤ 60.
- **[LOW]** The NSE client creates a new `AsyncClient` plus a homepage cookie fetch for each of 9 endpoints (`nse_client.py:99` …). **Fix:** one shared client with a warmed cookie jar, so O(2E) round trips become O(E+1).
- **[LOW]** Backtest hot loops (`backtest/runner.py:491, 586, 602-604, 828-837`): per-candle boolean masks, per-date filtering, `list.index`, `iterrows`, and the inherited per-tick DataFrame rebuild from P1.

### 2.3 Design and code smells

#### D1. [CRITICAL root cause] There is no broker interface, and the error contract is sentinel values. Verified (C1 and C2 are its symptoms).
- **Where:** `integrations/groww_client.py` (1,054 lines). The `get_kite_client = get_groww_client` alias is at l.1054. `kite_client.py` (377 lines, no importers) has yet another return shape.
- **Smells:**
  - Paper and live behaviour are `if settings.paper_trading` branches *inside* the client.
  - `get_ohlcv_snapshot` returns `{}` on any failure, which makes `_update_data_api_health`'s FORBIDDEN state effectively unreachable.
  - `increment` returns `-1`.
  - The safety-critical `_is_stock_uptrending` and circuit checks fail open.
- **Fix:**
  - Define a `BrokerPort` Protocol: `place_order(intent) -> OrderRef`, `place_stop(...) -> StopOrderRef`, `modify_stop`, `cancel(ref)`, `order_status(ref)`, `quotes(symbols) -> dict[str, Quote|None]`, `positions()`, `open_orders()`.
  - Use typed dataclasses and typed exceptions (`BrokerUnavailable`, `QuoteMissing`, `OrderRejected`, `AmbiguousOrderState`).
  - Provide `GrowwBroker` and `PaperBroker` implementations, with one contract test suite run against both.
  - Delete `kite_client.py` and the alias.

#### D2. [MEDIUM] The stop/target/sizing formula is copied in more than 6 places and has already drifted. Verified.
- **Where:**
  - `decision_engine.py:345-350, 745-755, 814-823`
  - `trading_agent.py:136-137` (% only), `:319-324`
  - `scanner.py:453-456`
  - `backtest/runner.py:75-78, 441-445` (with a "MUST stay in sync" comment)
- **Drift:**
  - Sizing uses ATR only when `n_candles ≥ 45`, but the SL uses ATR whenever ATR > 0, which is from 15 candles onward. Between 15 and 44 candles, quantity is sized for a 1% stop while an ATR×1.5 stop is placed, so actual risk ≠ `max_loss_per_trade_pct`.
  - The prompt says 0.8%/1.6% while config says 1.0%/2.0%.
  - `test_scanner.py:506-507, 529-530` still asserts 0.008/0.016.
  - Bounded impact: `_validate` overwrites whatever the LLM returns.
- **Fix:** a pure `domain/exit_plan.py` with `compute_exit_plan(entry, atr, n_candles, params) -> ExitPlan` and `size_position(plan, capital, params) -> int`, called from all six sites. The prompt numbers are rendered from it.

#### D3. [MEDIUM] Strategy thresholds are duplicated across the scanner, decision engine, backtest, system API and prompts. Verified, with corrections.
- **Where:** `scanner.py:617-622` (the comment says 42-68/45-65/48-63, the code uses 45-68/50-65/50-63; docstrings at :396 and :477 are stale), `decision_engine.py:539-546, 603-613, 865`, `api/routes/system.py:124-139` ("mirrors decision_engine"), `backtest/runner.py:69-89`
- **Correction:** the copies currently agree with each other. The drift is in the comments and docstrings, and the backtest's NEUTRAL-only bias is documented. The dead-zone values (2.0 in code, 2.5 in the prompt) are deliberate layering, not a contradiction.
- **Risk:** the next edit in one place silently diverges from the others.
- **Fix:** a frozen pydantic `StrategyParams` with `BiasProfile[(bias, stance)]`, injected into the scanner, decision engine, backtest and `/api/agent/status`. Generate the prompt's numeric text from it, and add a test that asserts the prompt contains the live thresholds.

#### D4. [MEDIUM] The backtest re-implements the signal gate instead of sharing it. Verified, with corrections.
- **Where:** `backtest/runner.py:345-470` vs `scanner.py:472-745`
- **Correction:** there is no drift today. The limitations (NEUTRAL only, no LLM, no trailing, partial or ROI-decay exits, no portfolio caps, no slippage or costs) are documented in `runner.py:33-41`.
- **Risk:** future divergence, Settings not being read, and a scanner-precision metric that can be mistaken for system P&L.
- **Fix:** a pure `SignalEvaluator.evaluate(snapshot, ctx, clock) -> GateResult`, built as a chain of named `Gate`s, plus a pure `ExitPolicy.evaluate(position, quote, now) -> Action`. Live and backtest both call them, and per-gate rejection telemetry comes for free.

#### Not independently verified
- **[MEDIUM]** God functions and god modules: `_check_signal` (about 270 lines), `_pre_check` (about 285), `_validate_decision` (about 190), `_poll` (about 175), `_book_partial` (about 170). Five modules exceed 1,000 lines (scanner 1218, decision_engine 1130, risk_manager 1087, groww_client 1054, research_agent 1030). **Fix:** split along feed / market_data / signals / risk_gates / llm_advisor / exit_policy / position_service / drawdown_monitor / eod_reporter boundaries, targeting functions under 50 lines and modules under 400.
- **[MEDIUM]** Layering violations:
  - Agents and routes call `get_db_context`, `get_redis` and `get_groww_client` directly (18 sites in risk_manager).
  - Literal Redis keys are used despite `core/redis_keys.py`: `stock_lock:{s}`, `consecutive_losses`, `trade_atr:{s}`, `agent:risk:*`.
  - `trades.py` imports the private `_transaction_costs`.
  - `system.py:424-450` reads `agent._scanner._stores`.
  - **Fix:** `TradeRepository`, a typed `RiskStateStore`, `domain/costs.py`, a public `Scanner.snapshot()`, and constructor injection from a composition root.
- **[MEDIUM]** Module-global mutable singletons shared across threads (`_ohlc_cache`, `global _data_api_forbidden`, the ltp_store dict + RLock, the manager singleton, lru-cached Settings) force a single worker. **Fix:** an `AppContainer` built in `lifespan` and passed through FastAPI dependencies.
- **[MEDIUM]** An anaemic domain model: one `trades` row serves as both position and trade, `quantity` is mutated to the remainder, statuses and reasons are free-form strings, `kite_order_id` is a legacy name, and no exit or SL order IDs are kept. **Fix:** a Position aggregate with child Orders/Fills rows carrying broker IDs and status, enums with CHECK constraints, and computed P&L.
- **[LOW]** Dead or misleading code:
  - `kite_client.py` and `token_refresh.py` (a no-op).
  - `MIS_FORCE_CLOSE` and `schedule_interval` are unused.
  - The NIFTY trend filter never activates, because `_ohlc_cache["NIFTY 50"]` is never populated and mock ticks have no token 2999.
  - `place_gtt` actually places a single-leg SL order.
  - `alpha_vantage_client` mostly calls Yahoo.
  - **Fix:** delete, rename, or wire them up, and add vulture or ruff to CI.
- **[LOW]** `IST = pytz.timezone(...)` is redefined in 14 modules, and `datetime.now(IST)` is called inline inside the gates, which makes them untestable. **Fix:** an injectable `Clock` and `zoneinfo`.

### 2.4 Reliability and security

#### R1. [HIGH] Live-order and broker-session endpoints are unauthenticated, and auth is opt-in and empty by default. Verified.
- **Where:**
  - `api/routes/trades.py:55` (`POST /trades/{id}/close` sends a live MARKET SELL)
  - `api/routes/auth.py:58, 124` (`/groww/login` mints a TOTP from the server secret when the body omits it; `/groww/logout` deletes the token)
  - `api/routes/market_brief.py:53` (a paid Sonnet run)
  - `system.py:56-66` (`_require_api_key` is a no-op when `admin_api_key == ""`, the default at `config.py:68`; it compares with `!=`)
- **Failure scenario:** anyone who can reach port 80 or 8000 can liquidate positions, log the broker out mid-session, or run up LLM spend. In the default config, halt, resume, start and stop are open too.
- **Fix:**
  - Refuse to boot with `paper_trading=False` and an empty key.
  - Put a router-level dependency on all non-GET routes and on `/ws/live`.
  - Use `secrets.compare_digest`.
  - Better: basic auth or OIDC at nginx, plus TLS.

#### R2. [HIGH] The prod compose `ports: []` does not remove the base port bindings. Verified.
- **Where:** `docker-compose.yml:14, 29, 48` publish 5432, 6379 and 8000. `docker-compose.prod.yml:18, 28, 46` use `ports: []`, and `:41` has `volumes: []`.
- **Defect:** Compose *concatenates* sequences, so clearing them requires `!reset []` (Compose ≥ 2.24). Redis has no `requirepass` and holds the broker access token (no TTL) and the HALT state. Postgres defaults to `changeme_postgres_password`. Docker NAT rules bypass the ufw rules in `deploy.sh:50-61`, and `deploy.sh` even suggests opening 8000 at the OCI layer. The `./backend:/app` bind mount also survives into prod.
- **Mitigation:** the default OCI security list only allows 22.
- **Fix:** move the ports into a dev-only `docker-compose.override.yml` bound to `127.0.0.1`, or use `!reset []`. Generate the Redis and Postgres secrets, and have `deploy.sh` reject `changeme`. Verify with `docker compose -f ... config` in CI.

#### R3. [MEDIUM] There is no heartbeat, watchdog or flatten-all control, and the health check cannot see trading liveness. Verified, severity reduced.
- **Where:** `api/routes/system.py:453` (always HTTP 200, with failures only in the body); `agent:risk:status` is set once and never refreshed.
- **Defect:** a dead RiskManager thread or a stalled feed with a responsive loop goes unnoticed. HALT only blocks entries, and stopping the agent *removes* monitoring while positions stay open.
- **Corrections:** a hung loop *does* fail the curl health check, through its timeout. Plain Docker with `restart: unless-stopped` does not restart unhealthy containers anyway. A per-position close button exists.
- **Fix:**
  - Heartbeat timestamps for risk, scanner and decision written to Redis.
  - `/api/health` returns 503 on a stale heartbeat or a DB/Redis failure during market hours.
  - An external dead-man ping (healthchecks.io or Telegram).
  - A protected `POST /api/trading/flatten` that cancels all open orders and exits net positions *taken from the broker*.

#### R4. [MEDIUM] Position and duplicate gates fail open on DB errors, and CLOSING rows are not counted as exposure. Verified, severity reduced.
- **Where:** `decision_engine.py:240-242` (`_count_open_positions` returns 0 on error), `:551-584` (duplicate and per-symbol checks only log)
- **Correction:** in a hard outage, C9 crashes the agent after one order, so the outcome is not "unlimited positions". In the transient case (a pool timeout followed by a successful insert), an extra or duplicate position is possible. Stuck CLOSING rows, where the SELL failed but shares are still live, never count toward the maximum.
- **Fix:** fail closed by returning `(False, "DB unavailable")`. Use one aggregated `FILTER` query, and count `OPEN + CLOSING` as exposure.

#### Not independently verified
- **[MEDIUM]** `/ws/live` has no auth and no Origin check (`api/websocket.py:122`), and it relays positions, P&L and LLM rationale. CORS does not cover WebSockets.
- **[MEDIUM]** The feed reauth loop is unbounded at a 2 s cadence (`scanner.py:1180`), and it races `_handle_failure`'s own reauth. **Fix:** cap at 3 attempts with backoff, serialize reauth behind an `asyncio.Lock` with a "recently reauthed" short-circuit.
- **[MEDIUM]** The Groww token is stored in Redis with no TTL (`auth.py:106`, `main.py:196-218`). Exceptions are echoed to clients (`detail=f"...{exc}"`). `echo=True` SQL logging is on by default because `app_env` defaults to `development` (`database.py:51`, `config.py:26`).
- **[MEDIUM]** The admin key is baked into the JS bundle or kept in localStorage (`frontend/src/app/core/interceptors/api-key.interceptor.ts:16`), and nginx serves it over plain HTTP. **Fix:** an HttpOnly session cookie or nginx basic auth, plus TLS.
- **[LOW]** Loose dependency pins (`growwapi>=1.0`, `anthropic==0.*`) with no lock file or hashes. Test dependencies ship in the image, and the base is `python:3.14-slim`. **Fix:** a uv or pip-tools lock with hashes, an exact pin on growwapi, and split dev dependencies.
- **[LOW]** The Android path binds uvicorn to `0.0.0.0` (`scripts/android/startup.sh:95`) and hardcodes the PG password (`install.sh:27`). **Fix:** bind to 127.0.0.1 (nginx already proxies), and generate a password at install with `chmod 600 .env`.

### 2.5 Data layer and tests

#### T1. [MEDIUM] The tests are largely tautological, and the money-handling paths have zero coverage. Verified.
- **Where:** `backend/tests/test_risk_manager.py:111-198` and others. TestExitDetection, TestDrawdown, TestTrailingSL, TestRoiDecay, TestEodReport and similar classes assert arithmetic on literals; for example `ltp = 2474.0; assert ltp <= open_trade.stop_loss_price`.
- **Gaps:** only about 8 tests call real RiskManager code (`_build_ltp_map`, `_get_trade_ltp`, `_book_partial`). No test calls `_close_position`, `_execute_trade`, `state_recovery`, `place_gtt` → persist, or `_check_daily_drawdown`. That is why C1, C2 and C4 shipped.
- **Fix:** delete or rewrite those tests to drive `_poll` with a `FakeBroker` and a real Postgres (testcontainers, with `alembic upgrade head` in a fixture). Add these scenario tests:
  - An SL hit produces exactly one SELL plus a cancel.
  - A manual close racing a poll.
  - A `place_order` timeout.
  - `get_ltp` failure produces no close.
  - A DB insert failure after a fill.
  - Recovery with a net-short position.
  - HALT behaviour during a Redis outage.
  - A mid-day restart preserves HALT.

#### Not independently verified
- **[MEDIUM]** A scanner test is wall-clock dependent (`tests/test_scanner.py:283`). `test_min_ticks_guard_blocks_early_signals` passes trivially after 15:15 or on a Friday afternoon, and no test asserts a positive `ScannerSignal`. **Fix:** inject the clock (the backtest already does, `runner.py:106-126`), use freezegun or time-machine, and add an end-to-end uptrend test.
- **[MEDIUM]** There is no CI, no pytest config, and no test DB. `test_api.py` uses `ASGITransport`, so lifespan never runs, and its 2 tests pass with Postgres and Redis down. conftest passes `kite_*` settings that are silently dropped. **Fix:** a GitHub Actions workflow with Postgres and Redis services running ruff, mypy, `alembic upgrade head`, `alembic check` and pytest with coverage gates on risk_manager, trading_agent and groww_client, plus `asyncio_mode="auto"`.
- **[MEDIUM]** `_book_partial`, `_update_stop_loss` and `_update_target` have no state guard (`risk_manager.py:693, 746, 764`), and `booked_pnl` is computed from a stale in-memory value. (Fix is listed under C4.)
- **[MEDIUM]** Money is stored as Float (`models/trade.py:38`, `daily_pnl.py:24-31`, migrations 001 and 006) and built up through repeated `round(x, 2)`. **Fix:** `Numeric(14,2)`, `Decimal` in the cost and P&L code, and prices quantized to the tick.
- **[MEDIUM]** There are no composite indexes for the queries that run every 5 s (`migrations/versions/001_initial.py:64-67`). Models and migrations have also drifted:
  - The MarketBrief model lacks `unique=True` while migration 002 adds it, so autogenerate would propose dropping it.
  - `booked_pnl` is nullable.
  - Migration 006's docstring says it is 004.
  - **Fix:** add `(trade_date, status)` and `(stock, trade_date)` indexes, make `booked_pnl NOT NULL DEFAULT 0`, use `ON CONFLICT` upserts, and run `alembic check` in CI.
- **[MEDIUM]** `entry_time`/`exit_time` are `Time` columns with no timezone (`models/trade.py:52-53`). `pnl.py:61` uses `date.today()`. **Fix:** `DateTime(timezone=True)` with `trade_date` derived in IST, plus a test frozen at 02:00 UTC.
- **[MEDIUM]** Redis key design:
  - `trade_atr:{stock}` is keyed per stock, not per trade, with a 24 h TTL (`trading_agent.py:425`).
  - The streak is a GET-then-SET instead of INCR, with no date scope (`risk_manager.py:526-528`).
  - The DB fallback for the streak has different semantics.
  - **Fix:** store ATR as a trade column, use date-scoped INCR counters, and put every key in `redis_keys.py` with a lint test.
- **[LOW]** The P&L route's orphan backfill scans all trade history (`pnl.py:103-117`). **Fix:** bound it by `days` and aggregate with SQL `GROUP BY` and a LEFT JOIN.
- **[LOW]** EOD reconciliation writes CLOSING rows as zero-P&L CLOSED without checking the broker (`risk_manager.py:881-904`). **Fix:** use the broker trade book, or mark the row `RECONCILE_PENDING` and alert.
- **[LOW]** Startup ordering in `deploy.sh`: `compose up` runs before `alembic upgrade head` (:92, :107). The research catch-up runs *inline* in `lifespan`, which blocks startup for 25-50 s during market hours (`main.py:310-331`).

---

## 3. Target architecture and refactor roadmap

### 3.1 Target shape

```
domain/            (pure, no IO, 100% unit-testable)
  strategy_params.py   StrategyParams + BiasProfile[(bias, stance)]   ← config
  exit_plan.py         compute_exit_plan(), size_position(), snap_to_tick()
  signal_gates.py      SignalEvaluator: [Gate] → GateResult            ← scanner + backtest
  exit_policy.py       ExitPolicy.evaluate(position, quote, now) → Action ← risk mgr + backtest
  costs.py             transaction_costs()
  indicators.py        incremental VWAP/RSI/EMA/MACD/ATR state machines
ports/
  broker.py            BrokerPort (typed refs, Quote|None, typed exceptions)
  clock.py             Clock.now_ist()
adapters/
  groww_broker.py  paper_broker.py  groww_feed.py  mock_feed.py
services/          (own IO and state transitions)
  position_service.py  open(intent) / close(id, reason) / book_partial(): CAS + outbox + idempotent orders
  reconciler.py        periodic broker⇄DB diff (positions + open orders)
  risk_state.py        HALT/locks/streak in Postgres (date-scoped), Redis cache, fail-closed reads
  supervisor.py        component restart/backoff, heartbeats, Telegram escalation
repositories/      TradeRepository, BriefRepository
app/container.py   composition root built in lifespan; FastAPI deps; no module singletons
```

**Invariants the target enforces:**
- Every order has a client reference. No write is retried blindly.
- Only `PositionService` changes position state, and only through CAS.
- The broker is the source of truth and the DB is reconciled to it.
- Safety gates fail closed.

### 3.2 Phased roadmap

| Phase | Goal | Items | Effort |
|---|---|---|---|
| **0. Live-safety blockers** (must finish before `paper_trading=False`) | Stop the money-losing paths | C1 persist the SL order ID (string column) and cancel+confirm before exits; C2 no `0.0` sentinel, skip/alert on missing or ≤0/stale LTP; C3 remove `_retry_sync` from order writes and add a client ref with an order-book check; C4 `.returning()` + early return, Phase-3 status guard, non-reentrant `_poll`; C5 terminal-status fill handling using `filled_quantity`; C6 tick-size snapping; C8 never reset HALT on startup or same-day sessions, fail-closed reads; R1 auth on close/login/logout/run/WS, refuse live mode without a key; R2 `!reset []` / dev-only ports, Redis password | **M** (about 1-2 weeks) |
| **1. Broker port and position ownership** | Kill the root causes | D1 `BrokerPort` + `GrowwBroker`/`PaperBroker` + contract tests; `PositionService` used by both the RiskManager and the API; outbox `PENDING` rows (C9); periodic `Reconciler` (C7); broker `modify_stop` on trailing/breakeven; delete `kite_client.py` and `token_refresh.py` | **M-L** |
| **2. Supervision and observability** | Never fail silently | C10 independent RiskManager plus a scanner supervisor; heartbeats, a 503 health check and a dead-man ping (R3); a `/flatten` endpoint; Telegram on any component exit; EOD report as its own cron job; bounded fallback buffer | **M** |
| **3. Domain extraction and performance** | One strategy, fast hot path | D2/D3/D4 `StrategyParams`, `exit_plan`, `SignalEvaluator`, `ExitPolicy` shared with the backtest; P1 incremental indicators and session VWAP; P3 event-driven SL checks from push LTP; P2 dedicated executors, batched quotes, async backoff; ticks marshalled to the loop; injectable `Clock`; split the god functions | **L** |
| **4. Data model and platform hygiene** | Correct books, reproducible builds | Position→Orders/Fills model, `Numeric` money, enums, composite indexes, tz-aware timestamps, migration drift fixes; risk state in Postgres; Redis pipelines and MGET without per-call PING; lock file with hashes; CI (ruff, mypy, alembic check, pytest against real Postgres/Redis, coverage gates); rewrite the tautological tests; load the holiday calendar dynamically and fail closed when it is empty | **L** |

---

## 4. Top 10 engineering actions (in priority order)

1. **Persist and cancel the broker SL order.** Store the raw Groww order ID as a string. Before every exit, cancel and confirm, or skip the SELL if the stop already executed. (C1, effort S)
2. **Remove the `0.0` LTP sentinel.** `get_ltp` returns `None` or raises, and the RiskManager skips missing, non-positive or stale quotes and alerts. (C2, effort S)
3. **Make order placement idempotent.** No auto-retry on writes; send a per-intent client reference; check the order book on ambiguous errors. (C3, effort S-M)
4. **Enforce the CAS.** Use `.returning(Trade.id)` with an early return in `_close_position`, put status guards on every RiskManager update, and make `_poll` non-reentrant. (C4, effort S)
5. **Handle fills properly.** Wait for a terminal status, act on `filled_quantity`, and place no SL or row for a rejected order. Snap all prices to tick size. (C5 + C6, effort S-M)
6. **Make risk state durable and fail-closed.** Use a date-scoped HALT, streak and locks in Postgres; never reset on a startup or same-day session; an unreadable state means HALT. (C8 + R4, effort M)
7. **Lock down the surface.** Auth on every mutating route and on the WebSocket, refuse live mode without a key, fix the prod compose port and volume merge, set Redis and Postgres secrets, and enable TLS. (R1 + R2, effort S-M)
8. **Add a broker reconciler and an outbox.** Write a `PENDING` row before the order, run a 30-60 s broker⇄DB diff for positions and open orders, and alert or halt on net shorts. (C7 + C9, effort M)
9. **Supervise and observe.** Run the RiskManager independently of the scanner, add a scanner restart supervisor, heartbeats, a 503 health check, a dead-man ping, Telegram escalation, and a `/flatten` endpoint. (C10 + R3, effort M)
10. **Introduce `BrokerPort` and the shared domain, and write real tests against them.** Add a `FakeBroker` and testcontainers-based scenario tests for SL-once, manual-close race, timeout, LTP failure, restart-preserves-HALT and net-short recovery; set up CI; then move to incremental indicators and a backtest that shares `SignalEvaluator` and `ExitPolicy`. (D1-D4, T1, P1, effort L)

*Note on verification: one disagreement between reviewers was resolved by re-reading the code. `_retry_sync` (`groww_client.py:76-99`) calls `time.sleep(2**attempt)` after every failed attempt, including the last, so worst-case blocking is 2+4+8 = 14 s per wrapped call. The "2/4 s only" reading was incorrect.*
