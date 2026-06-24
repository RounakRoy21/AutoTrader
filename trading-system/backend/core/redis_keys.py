"""
Centralised Redis key constants.

All modules must import keys from here — never define key strings inline.
This prevents silent mismatches caused by copy-paste typos and makes
cross-module key dependencies explicit and searchable.
"""

# ── Trading control ────────────────────────────────────────────────────────────
HALT_KEY = "trading_halt"               # "TRUE" | "FALSE"

# ── Groww authentication ──────────────────────────────────────────────────────
GROWW_TOKEN_KEY = "groww_session_token"  # Groww access token (no expiry, persists until revoked)

# ── Agent status ───────────────────────────────────────────────────────────────
RESEARCH_STATUS_KEY = "agent:research:status"   # "ACTIVE" | "INACTIVE" | "ERROR"
TRADING_STATUS_KEY = "agent:trading:status"     # "ACTIVE" | "INACTIVE"
RISK_STATUS_KEY = "agent:risk:status"           # "ACTIVE" | "INACTIVE"

# ── Agent counters / metadata ─────────────────────────────────────────────────
DAILY_TRADE_COUNT_KEY = "daily_trade_count"
RESEARCH_STEP_KEY = "agent:research:step"
RESEARCH_LAST_BIAS_KEY = "agent:research:last_bias"
RESEARCH_LAST_CONFIDENCE_KEY = "agent:research:last_confidence"
RESEARCH_LAST_RUN_STARTED_KEY = "agent:research:last_run_started"
RESEARCH_LAST_RUN_COMPLETED_KEY = "agent:research:last_run_completed"
TRADING_LAST_SIGNAL_STOCK_KEY = "agent:trading:last_signal_stock"
TRADING_LAST_SIGNAL_TIME_KEY = "agent:trading:last_signal_time"
RISK_DAILY_LOSS_KEY = "agent:risk:daily_loss"
RISK_DRAWDOWN_PCT_KEY = "agent:risk:drawdown_pct"

# ── Market data ────────────────────────────────────────────────────────────────
LATEST_MARKET_BRIEF_KEY = "latest_market_brief"

# Today's LLM-chosen watchlist — written by the Research Agent after each
# successful run, read by load_instrument_map() so the scanner subscribes
# to exactly the stocks the agent flagged.  TTL = 24 h (one trading day).
TODAY_WATCHLIST_KEY = "today_watchlist"

# ── Decision feed ─────────────────────────────────────────────────────────────
# Rolling list of the last 100 decision engine events (pre-check rejections +
# LLM decisions).  Stored as a Redis list (LPUSH / LTRIM); each entry is JSON.
DECISION_FEED_KEY = "decision_feed"

# ── Instrument map ─────────────────────────────────────────────────────────────
# Cached symbol→token map built from Groww NSE instrument list.  TTL = 24 h.
# Invalidated by the Research Agent after each brief so load_instrument_map()
# re-fetches with the new watchlist before the 09:15 trading session starts.
INSTRUMENT_MAP_KEY = "groww_instrument_map"

# ── Anthropic API call counters (daily, auto-reset via TTL) ────────────────────
# research = claude-sonnet calls (market brief generation, ~2/day)
# decision = claude-haiku calls (trade decision engine, ~0–20/day in live mode)
# TTL is set to 24 h on first increment so they reset automatically each day.
ANTHROPIC_CALLS_RESEARCH_KEY = "anthropic_calls:today:research"
ANTHROPIC_CALLS_DECISION_KEY = "anthropic_calls:today:decision"

# ── Market-data API health ─────────────────────────────────────────────────────
# Tracks whether the Groww Live-Data / Historical REST API groups are reachable.
# Written by the scanner OHLCV poll loop; read by /api/health and /api/agent/status
# so the dashboard can surface a "data feed forbidden — trading paused" banner
# instead of silently producing zero signals.
DATA_API_STATUS_KEY = "data_api:status"      # "OK" | "FORBIDDEN" | "DEGRADED"
DATA_API_DETAIL_KEY = "data_api:detail"      # last error message (human readable)
DATA_API_LAST_OK_KEY = "data_api:last_ok"    # ISO timestamp of last successful poll

# ── Scanner warmup ────────────────────────────────────────────────────────────
# Written by the scanner the moment GrowwFeed connects.  The dashboard derives a
# progress percentage from now − feed_connected_at so the user can see how many
# of the required 15 candles have accumulated instead of seeing "no signals" and
# assuming something is broken.  TTL = 86400 s (auto-expires at midnight).
SCANNER_FEED_CONNECTED_AT_KEY = "scanner:feed_connected_at"  # ISO-8601 UTC string

