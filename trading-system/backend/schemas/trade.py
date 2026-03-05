"""
Pydantic schemas for trade records and scanner signals.
"""

from __future__ import annotations

from datetime import date, time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────

class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProductType(str, Enum):
    MIS = "MIS"
    CNC = "CNC"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitReason(str, Enum):
    TARGET_HIT = "TARGET_HIT"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    MANUAL = "MANUAL"
    EOD_CLOSE = "EOD_CLOSE"


# ── Scanner Signal ─────────────────────────────────

class ScannerSignal(BaseModel):
    """Signal emitted by the Scanner when all three entry conditions are met."""
    stock: str
    exchange: str = "NSE"
    signal_time: str  # HH:MM:SS
    ltp: float
    vwap: float
    rsi: float = Field(ge=0, le=100)
    volume_ratio: float
    suggested_qty: int = Field(ge=1)
    # Candle-based indicators (populated when enough data is available)
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    macd_histogram: Optional[float] = None
    atr: Optional[float] = None
    rsi_5m: Optional[float] = None    # higher timeframe RSI (5-minute candles)


# ── Trade Request / Response ───────────────────────

class TradeCreate(BaseModel):
    """Schema for creating a new trade record after order confirmation."""
    kite_order_id: Optional[str] = None
    stock: str
    exchange: str = "NSE"
    direction: Direction
    product_type: ProductType
    quantity: int
    entry_price: float
    stop_loss_price: float
    target_price: float
    trade_date: date
    entry_time: time
    decision_rationale: Optional[str] = None


class TradeResponse(BaseModel):
    """Schema for trade records returned by the API."""
    id: int
    kite_order_id: Optional[str]
    stock: str
    exchange: str
    direction: str
    product_type: str
    quantity: int
    entry_price: float
    stop_loss_price: float
    target_price: float
    exit_price: Optional[float]
    exit_reason: Optional[str]
    realized_pnl: Optional[float]
    status: str
    trade_date: date
    entry_time: time
    exit_time: Optional[time]
    decision_rationale: Optional[str]

    class Config:
        from_attributes = True


class TradeCloseRequest(BaseModel):
    """Schema for closing a trade (exit)."""
    exit_price: float
    exit_reason: ExitReason
    exit_time: time
