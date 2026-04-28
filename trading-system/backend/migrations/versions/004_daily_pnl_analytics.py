"""Add analytics columns to daily_pnl table

profit_factor, sharpe_ratio, avg_trade_duration_min, max_consecutive_losses
were computed in _generate_eod_report() but only sent to Telegram/logs.
This migration persists them so they are queryable for strategy analysis.

Revision ID: 004_daily_pnl_analytics
Revises: 003_add_gtt_trigger_id
Create Date: 2026-04-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_daily_pnl_analytics"
down_revision: Union[str, None] = "003_add_gtt_trigger_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("daily_pnl", sa.Column("profit_factor", sa.Float(), nullable=True))
    op.add_column("daily_pnl", sa.Column("sharpe_ratio", sa.Float(), nullable=True))
    op.add_column("daily_pnl", sa.Column("avg_trade_duration_min", sa.Float(), nullable=True))
    op.add_column("daily_pnl", sa.Column("max_consecutive_losses", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("daily_pnl", "max_consecutive_losses")
    op.drop_column("daily_pnl", "avg_trade_duration_min")
    op.drop_column("daily_pnl", "sharpe_ratio")
    op.drop_column("daily_pnl", "profit_factor")
