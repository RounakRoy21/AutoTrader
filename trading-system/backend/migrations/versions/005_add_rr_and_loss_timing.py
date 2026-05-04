"""Add avg_realised_rr and loss time distribution columns to daily_pnl.

Revision ID: 005_add_rr_and_loss_timing
Revises: 004_daily_pnl_analytics
"""

from alembic import op
import sqlalchemy as sa

revision = "005_add_rr_and_loss_timing"
down_revision = "004_daily_pnl_analytics"


def upgrade() -> None:
    op.add_column("daily_pnl", sa.Column("avg_realised_rr", sa.Float(), nullable=True))
    op.add_column("daily_pnl", sa.Column("losses_before_1030", sa.Integer(), nullable=True))
    op.add_column("daily_pnl", sa.Column("losses_1030_to_1330", sa.Integer(), nullable=True))
    op.add_column("daily_pnl", sa.Column("losses_after_1330", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("daily_pnl", "losses_after_1330")
    op.drop_column("daily_pnl", "losses_1030_to_1330")
    op.drop_column("daily_pnl", "losses_before_1030")
    op.drop_column("daily_pnl", "avg_realised_rr")
