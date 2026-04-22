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
KITE_TOKEN_KEY = GROWW_TOKEN_KEY          # backward-compat alias (kite_client.py)

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
