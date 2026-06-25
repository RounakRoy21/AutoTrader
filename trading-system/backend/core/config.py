"""
Application configuration loaded from environment variables via pydantic-settings.
All secrets are sourced from the .env file — nothing is hardcoded.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the AutoTrader backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"
    paper_trading: bool = True
    paper_risk_use_broker_ltp: bool = True

    # ── PostgreSQL ─────────────────────────────────
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "autotrader"
    postgres_user: str = "autotrader"
    postgres_password: str = "changeme_postgres_password"
    database_url: str = "postgresql+asyncpg://autotrader:changeme_postgres_password@postgres:5432/autotrader"
    database_url_sync: str = "postgresql://autotrader:changeme_postgres_password@postgres:5432/autotrader"

    # ── Redis ──────────────────────────────────────
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    redis_url: str = "redis://redis:6379/0"

    # ── Groww API ─────────────────────────────────
    groww_client_id: str = ""
    groww_password: str = ""
    groww_totp_secret: str = ""  # Base32 TOTP secret for pyotp

    # ── Anthropic (Claude) ────────────────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"           # Research Agent
    anthropic_decision_model: str = "claude-haiku-4-5-20251001"  # Decision Engine

    # ── Alpha Vantage ─────────────────────────────
    alpha_vantage_api_key: str = ""

    # ── Telegram ──────────────────────────────────
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # ── API Security ──────────────────────────
    # Required in production: set ADMIN_API_KEY in .env to a strong random secret.
    # All trading-control endpoints (halt, resume, start, stop) require the caller
    # to supply this value in the X-Api-Key request header when non-empty.
    # An empty string disables the check (development / paper-trading only).
    admin_api_key: str = ""
    # ── CORS ──────────────────────────────────────
    cors_origins: str = "http://localhost:4200,http://localhost:4201"

    # ── Trading Configuration ─────────────────────
    total_capital: float = 1_000_000.0
    core_bucket_pct: float = 0.70
    hedge_bucket_pct: float = 0.20
    warchest_bucket_pct: float = 0.10
    max_loss_per_trade_pct: float = 0.015
    stop_loss_pct: float = 0.010             # 1.0% fixed fallback (NSE large-cap ATR noise floor)
    min_target_pct: float = 0.020             # 2.0% fixed fallback (maintains 2:1 R:R vs 1.0% SL)
    trailing_sl_activation_pct: float = 0.008  # activate trail at 0.8% above entry
    trailing_sl_trail_pct: float = 0.007       # trail distance: 0.7% below LTP
    atr_sl_multiplier: float = 1.5             # SL = entry − ATR × multiplier
    atr_target_multiplier: float = 3.0         # target = entry + ATR × multiplier (2:1 RR)
    stock_lock_after_sl: bool = True           # lock stock after SL hit for rest of day
    consecutive_loss_pause_threshold: int = 3  # pause after N consecutive SL hits
    consecutive_loss_pause_minutes: int = 30   # pause duration in minutes
    roi_decay_enabled: bool = True             # reduce target over time if not hit
    # ── Partial profit-booking / scale-out ────────
    # Book a fraction of the position once price reaches a multiple of the initial
    # risk (R = entry − initial stop).  Locks in profit on momentum spikes and,
    # when move-to-breakeven is enabled, makes the remaining position risk-free —
    # smoothing the equity curve without capping the upside on the runner.
    partial_booking_enabled: bool = True
    partial_booking_trigger_r: float = 1.0     # book when price reaches entry + R × this
    partial_booking_fraction: float = 0.5      # fraction of qty to book (0 < f < 1)
    partial_booking_move_sl_to_breakeven: bool = True  # raise SL to entry after booking
    max_open_positions: int = 3
    max_trades_per_day: int = 6
    max_trades_per_symbol_per_day: int = 2   # cap repeat entries in same stock (breadth > concentration)
    daily_drawdown_soft_alert_pct: float = 0.02   # 2% soft warning tier (Telegram alert, no halt)
    daily_drawdown_limit_pct: float = 0.03         # 3% hard halt
    # SH2: Gap-at-open filter — reject signals when stock gapped up more than
    # this percentage vs previous close.  NSE intraday longs on >1.5% gap-up
    # stocks have a high false-signal rate due to early mean-reversion pressure.
    gap_filter_pct: float = 1.5

    # A2: Intraday NIFTY 50 trend filter — suppress all long signals when the index
    # has drifted below this percentage from its session open price.
    # -0.5% is the minimum meaningful drift that indicates broad market selling.
    nifty_trend_filter_pct: float = -0.005

    # ── Derived helpers ───────────────────────────
    @property
    def core_capital(self) -> float:
        return self.total_capital * self.core_bucket_pct

    @property
    def hedge_capital(self) -> float:
        return self.total_capital * self.hedge_bucket_pct

    @property
    def warchest_capital(self) -> float:
        return self.total_capital * self.warchest_bucket_pct

    @property
    def max_loss_per_trade(self) -> float:
        return self.total_capital * self.max_loss_per_trade_pct

    @property
    def daily_drawdown_limit(self) -> float:
        return self.total_capital * self.daily_drawdown_limit_pct

    # ── NIFTY 50 focus stocks ────────────────────
    focus_stocks: List[str] = Field(
        default=["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK", "BHARTIARTL"]
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
