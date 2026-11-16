"""Add entry_order_ref to trades (outbox / pending-intent entries)

Audit C9: the Trade row was written only after the broker BUY and its stop,
so a DB failure (or a crash / cancellation) after the fill left a live
position with no row and no management.  Entries now insert a ``PENDING`` row
carrying the order_reference_id BEFORE the order is sent, then move it to
OPEN (filled) or REJECTED (nothing filled).  The reconciler resolves stranded
PENDING rows through the broker by this reference.  ``status`` is a plain
VARCHAR(10), so PENDING / REJECTED need no enum change.

Revision ID: 009_add_entry_order_ref
Revises: 008_add_stop_kind
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009_add_entry_order_ref"
down_revision: Union[str, None] = "008_add_stop_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("entry_order_ref", sa.String(32), nullable=True))
    op.create_index("ix_trades_entry_order_ref", "trades", ["entry_order_ref"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_trades_entry_order_ref", table_name="trades")
    op.drop_column("trades", "entry_order_ref")
