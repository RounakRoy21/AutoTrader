"""Add stop_kind to trades (how the stop was last moved)

Audit R14: trailing-stop and breakeven exits were labelled STOP_LOSS_HIT, so
profitable exits locked the stock, advanced the loss streak and sent "Loss"
alerts.  ``stop_kind`` records why the stop was last moved (NULL = initial
stop, TRAILING, BREAKEVEN) so a stop-out is labelled STOP_LOSS_HIT,
TRAILING_SL or BREAKEVEN.  ``exit_reason`` is a plain VARCHAR(20), so the new
reason values need no enum change.

Revision ID: 008_add_stop_kind
Revises: 007_add_sl_order_id
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_add_stop_kind"
down_revision: Union[str, None] = "007_add_sl_order_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("stop_kind", sa.String(12), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "stop_kind")
