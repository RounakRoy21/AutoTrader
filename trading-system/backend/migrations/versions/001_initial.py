"""Initial schema — all four tables

Revision ID: 001_initial
Revises: None
Create Date: 2026-02-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── market_briefs ─────────────────────────────
    op.create_table(
        "market_briefs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.Time(), nullable=False),
        sa.Column("market_bias", sa.String(10), nullable=False),
        sa.Column("bias_confidence", sa.Float(), nullable=False),
        sa.Column("sgx_nifty_signal", sa.String(20), nullable=True),
        sa.Column("fii_signal", sa.String(20), nullable=True),
        sa.Column("dxy_signal", sa.String(30), nullable=True),
        sa.Column("us_markets_signal", sa.String(20), nullable=True),
        sa.Column("watchlist", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("avoid_list", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("recommended_stance", sa.String(30), nullable=True),
        sa.Column("raw_json", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_briefs")),
    )
    op.create_index(op.f("ix_market_briefs_date"), "market_briefs", ["date"])

    # ── trades ────────────────────────────────────
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kite_order_id", sa.String(50), nullable=True),
        sa.Column("stock", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(10), nullable=False),
        sa.Column("direction", sa.String(4), nullable=False),
        sa.Column("product_type", sa.String(5), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("stop_loss_price", sa.Float(), nullable=False),
        sa.Column("target_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(20), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("entry_time", sa.Time(), nullable=False),
        sa.Column("exit_time", sa.Time(), nullable=True),
        sa.Column("decision_rationale", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trades")),
    )
    op.create_index(op.f("ix_trades_kite_order_id"), "trades", ["kite_order_id"])
    op.create_index(op.f("ix_trades_stock"), "trades", ["stock"])
    op.create_index(op.f("ix_trades_status"), "trades", ["status"])
    op.create_index(op.f("ix_trades_trade_date"), "trades", ["trade_date"])

    # ── daily_pnl ─────────────────────────────────
    op.create_table(
        "daily_pnl",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("starting_capital", sa.Float(), nullable=False),
        sa.Column("ending_capital", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("total_trades", sa.Integer(), nullable=False),
        sa.Column("winning_trades", sa.Integer(), nullable=False),
        sa.Column("losing_trades", sa.Integer(), nullable=False),
        sa.Column("return_pct", sa.Float(), nullable=False),
        sa.Column("trading_halted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_pnl")),
        sa.UniqueConstraint("date", name=op.f("uq_daily_pnl_date")),
    )
    op.create_index(op.f("ix_daily_pnl_date"), "daily_pnl", ["date"])

    # ── agent_logs ────────────────────────────────
    op.create_table(
        "agent_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_name", sa.String(30), nullable=False),
        sa.Column("log_level", sa.String(10), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_logs")),
    )
    op.create_index(op.f("ix_agent_logs_agent_name"), "agent_logs", ["agent_name"])
    op.create_index(op.f("ix_agent_logs_log_level"), "agent_logs", ["log_level"])


def downgrade() -> None:
    op.drop_table("agent_logs")
    op.drop_table("daily_pnl")
    op.drop_table("trades")
    op.drop_table("market_briefs")
