"""Rename kite_order_id → entry_order_id; drop gtt_trigger_id

Both are leftovers of the Zerodha Kite integration.  ``kite_order_id`` has
held the Groww entry order id since the broker switch; ``gtt_trigger_id`` (the
truncated synthetic stop id, superseded by ``sl_order_id`` in 007) is no
longer read or written.

Revision ID: 010_drop_kite_legacy
Revises: 009_add_entry_order_ref
Create Date: 2026-11-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_drop_kite_legacy"
down_revision: Union[str, None] = "009_add_entry_order_ref"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_trades_kite_order_id", table_name="trades")
    op.alter_column("trades", "kite_order_id", new_column_name="entry_order_id")
    op.create_index("ix_trades_entry_order_id", "trades", ["entry_order_id"])
    op.drop_column("trades", "gtt_trigger_id")


def downgrade() -> None:
    op.add_column("trades", sa.Column("gtt_trigger_id", sa.Integer(), nullable=True))
    op.drop_index("ix_trades_entry_order_id", table_name="trades")
    op.alter_column("trades", "entry_order_id", new_column_name="kite_order_id")
    op.create_index("ix_trades_kite_order_id", "trades", ["kite_order_id"])
