"""Add unique constraint on market_briefs.date

Revision ID: 002_market_briefs_unique_date
Revises: 001_initial
Create Date: 2026-03-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002_market_briefs_unique_date"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enforce one brief per trading day so the research-agent upsert
    # cannot produce duplicates even under concurrent triggers.
    op.create_unique_constraint(
        "uq_market_briefs_date", "market_briefs", ["date"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_market_briefs_date", "market_briefs", type_="unique")
