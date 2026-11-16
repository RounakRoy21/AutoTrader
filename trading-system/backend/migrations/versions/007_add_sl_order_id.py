"""Add sl_order_id (full raw broker stop-loss order id) to trades

Audit R1 / C1: the broker SL order id was truncated to an 8-digit integer
(``gtt_trigger_id``) and in practice never persisted at all, so no exit path
could cancel the resting SL SELL.  The full raw Groww order id is now stored
as a string and used for every cancel / modify / status call.
``gtt_trigger_id`` is kept (legacy, unused by new code) so old rows still load.

Revision ID: 007_add_sl_order_id
Revises: 006_add_partial_booking
Create Date: 2026-11-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_add_sl_order_id"
down_revision: Union[str, None] = "006_add_partial_booking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("sl_order_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "sl_order_id")
