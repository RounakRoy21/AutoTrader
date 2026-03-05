"""Add gtt_trigger_id column to trades table

Stores the Zerodha GTT trigger ID so it can be cancelled when the
position is closed (target hit, EOD close, etc.), preventing orphaned
GTT orders from creating naked short positions.

Revision ID: 003_add_gtt_trigger_id
Revises: 002_market_briefs_unique_date
Create Date: 2026-03-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_add_gtt_trigger_id"
down_revision: Union[str, None] = "002_market_briefs_unique_date"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("gtt_trigger_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "gtt_trigger_id")
