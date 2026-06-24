"""Add partial profit-booking / scale-out columns to trades table

Supports booking a fraction of a position once price reaches a multiple of the
initial risk (R).  The remaining position keeps running (optionally with the stop
moved to breakeven), and the booked leg's net P&L is accumulated in booked_pnl so
it can be folded into realized_pnl when the position fully closes.

  * original_quantity     — full entry size, preserved once `quantity` is reduced
  * partial_target_price  — price at which the scale-out fires (immutable)
  * partial_booked        — guard so the scale-out only fires once
  * booked_pnl            — net realised P&L of the booked leg(s)

Revision ID: 004_add_partial_booking
Revises: 003_add_gtt_trigger_id
Create Date: 2026-03-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_add_partial_booking"
down_revision: Union[str, None] = "005_add_rr_and_loss_timing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("original_quantity", sa.Integer(), nullable=True))
    op.add_column("trades", sa.Column("partial_target_price", sa.Float(), nullable=True))
    op.add_column(
        "trades",
        sa.Column(
            "partial_booked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "trades",
        sa.Column(
            "booked_pnl",
            sa.Float(),
            nullable=True,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("trades", "booked_pnl")
    op.drop_column("trades", "partial_booked")
    op.drop_column("trades", "partial_target_price")
    op.drop_column("trades", "original_quantity")
