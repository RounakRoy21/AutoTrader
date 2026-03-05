"""
Alembic env.py — configures the migration environment.
Uses the async SQLAlchemy engine for online migrations.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add the backend directory to sys.path so models can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.database import Base
from core.config import get_settings

# Import all models so they register with Base.metadata
from models.market_brief import MarketBrief  # noqa: F401
from models.trade import Trade  # noqa: F401
from models.daily_pnl import DailyPnl  # noqa: F401
from models.agent_log import AgentLog  # noqa: F401

config = context.config

# Load settings and override the sqlalchemy.url
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL script generation)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (direct database connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
