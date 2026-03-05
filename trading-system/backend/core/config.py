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

    # ── Zerodha Kite Connect ──────────────────────
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_redirect_url: str = "http://localhost:8000/api/auth/kite/callback"

    # ── Anthropic (Claude) ────────────────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    # ── Alpha Vantage ─────────────────────────────
    alpha_vantage_api_key: str = ""

    # ── NewsAPI ───────────────────────────────────
    newsapi_api_key: str = ""

    # ── Telegram ──────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── CORS ──────────────────────────────────────
    cors_origins: str = "http://localhost:4200,http://localhost:4201"

    # ── Trading Configuration ─────────────────────
    total_capital: float = 1_000_000.0
    core_bucket_pct: float = 0.70
    hedge_bucket_pct: float = 0.20
    warchest_bucket_pct: float = 0.10
    max_loss_per_trade_pct: float = 0.015
    stop_loss_pct: float = 0.008
    min_target_pct: float = 0.016
    trailing_sl_activation_pct: float = 0.005  # activate trail at 0.5% above entry
    trailing_sl_trail_pct: float = 0.005       # trail distance: 0.5% below LTP
    atr_sl_multiplier: float = 1.5             # SL = entry − ATR × multiplier
    atr_target_multiplier: float = 3.0         # target = entry + ATR × multiplier (2:1 RR)
    stock_lock_after_sl: bool = True           # lock stock after SL hit for rest of day
    consecutive_loss_pause_threshold: int = 3  # pause after N consecutive SL hits
    consecutive_loss_pause_minutes: int = 30   # pause duration in minutes
    roi_decay_enabled: bool = True             # reduce target over time if not hit
    max_open_positions: int = 3
    max_trades_per_day: int = 6
    daily_drawdown_limit_pct: float = 0.03

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
