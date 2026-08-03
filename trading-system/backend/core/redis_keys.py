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

# ── News aggregator health ────────────────────────────────────────────────────
# Per-source health snapshot written by HybridNewsAggregator.check_feed_health()
# (6 AM probe + on-demand refresh).  JSON blob with each RSS/Google-News source's
# status (OK / DOWN / STALE), fresh-item count, last-OK timestamp and last error.
# Read by GET /api/news/health so the operator can see at a glance which sources
# are healthy and catch a silently-broken RSS URL before news quality degrades.
# No TTL — the snapshot persists across restarts and is overwritten each check.
NEWS_HEALTH_KEY = "news:health"

# ── Decision feed ─────────────────────────────────────────────────────────────
# Rolling list of the last 100 decision engine events (pre-check rejections +
# LLM decisions).  Stored as a Redis list (LPUSH / LTRIM); each entry is JSON.
DECISION_FEED_KEY = "decision_feed"

# ── Instrument map ─────────────────────────────────────────────────────────────
# Cached symbol→token map built from Groww NSE instrument list.  TTL = 24 h.
# Invalidated by the Research Agent after each brief so load_instrument_map()
# re-fetches with the new watchlist before the 09:15 trading session starts.
INSTRUMENT_MAP_KEY = "groww_instrument_map"

# ── Anthropic API call counters (daily, per IST calendar day) ─────────────────
# research = claude-sonnet calls (market brief generation, ~2/day)
# decision = claude-haiku calls (trade decision engine, ~0–20/day in live mode)
# Keys are date-stamped in IST so each calendar day gets its own key and stale
# counts from yesterday never bleed into today's display.
# TTL is 48 h so keys survive across weekends and are eventually cleaned up.
def anthropic_calls_research_key() -> str:
    """Return today's IST-date-stamped Redis key for research (Sonnet) call count."""
    from datetime import datetime
    import pytz
    return f"anthropic_calls:{datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%Y-%m-%d')}:research"


def anthropic_calls_decision_key() -> str:
    """Return today's IST-date-stamped Redis key for decision (Haiku) call count."""
    from datetime import datetime
    import pytz
    return f"anthropic_calls:{datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%Y-%m-%d')}:decision"


# Kept for any external tooling that reads these names — no longer written to.
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

# Set to "TRUE" whenever the NIFTY 50 intraday trend filter is actively suppressing
# all long signals (NIFTY drifted below the threshold from its session open).
# Cleared to "FALSE" when the first signal passes the filter — i.e. NIFTY recovered.
# TTL = 86400 s (auto-expires overnight so it doesn't carry across sessions).
# Written by scanner.py; read by /api/agent/status for dashboard surfacing.
SCANNER_NIFTY_FILTER_KEY = "scanner:nifty_filter_active"  # "TRUE" | "FALSE"

# ── NIFTY 50 constituent monitor ──────────────────────────────────────────────
# Written by agents/nifty50_monitor.check_nifty50_drift() (monthly job).  Tracks
# the official index membership so the operator is alerted when stocks enter/leave
# the NIFTY 50 or a demerger/split occurs and the hardcoded ticker maps need a
# refresh.  No TTL — these persist across restarts so drift is detected against
# the last-seen snapshot.
NIFTY50_CONSTITUENTS_KEY = "nifty50:constituents"   # JSON: last-seen live symbols + companies + ts
NIFTY50_DRIFT_KEY = "nifty50:drift_report"          # JSON: latest diff report
NIFTY50_LAST_CHECK_KEY = "nifty50:last_check"       # ISO-8601 IST timestamp of last check

