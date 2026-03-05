"""
Pydantic schemas for the Decision Engine LLM output.
Validates every LLM decision before any trade is placed.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Decision(str, Enum):
    EXECUTE = "EXECUTE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


class ProductType(str, Enum):
    MIS = "MIS"
    CNC = "CNC"


class SignalAudit(BaseModel):
    """
    Quantified evidence the LLM must provide for every decision.

    Every factual field (rsi_cited, volume_ratio_cited, vwap_deviation_pct,
    ema_aligned, macd_confirms, risk_reward_ratio) is OVERWRITTEN by
    _validate_decision() from the raw signal — the LLM cannot fudge them.
    The one field the system trusts from the LLM verbatim is confidence_score,
    which is why it maps directly to the EXECUTE / REDUCE / REJECT outcome.
    """

    # ── RSI ──────────────────────────────────────────────────────────────────
    rsi_cited: float = Field(
        description="The 1-min RSI value from the signal (copied exactly — will be verified)."
    )
    rsi_in_range: bool = Field(
        description=(
            "True if RSI is inside the valid entry zone for this direction. "
            "LONG: 40 ≤ RSI ≤ 72.  SHORT: 28 ≤ RSI ≤ 60."
        )
    )

    # ── Volume ───────────────────────────────────────────────────────────────
    volume_ratio_cited: float = Field(
        description="Volume ratio from the signal (copied exactly — will be verified)."
    )
    volume_confirms: bool = Field(
        description="True if volume_ratio ≥ 1.5 (minimum threshold for signal validity)."
    )

    # ── VWAP ─────────────────────────────────────────────────────────────────
    vwap_deviation_pct: float = Field(
        description=(
            "Deviation of price from VWAP as a percentage: "
            "(price − VWAP) / VWAP × 100.  Positive = price above VWAP."
        )
    )
    price_vwap_valid: bool = Field(
        description=(
            "True if the VWAP relationship is correct for this direction. "
            "LONG: 0 % < deviation ≤ 1.5 %.  SHORT: −1.5 % ≤ deviation < 0 %."
        )
    )

    # ── EMA ──────────────────────────────────────────────────────────────────
    ema_aligned: Optional[bool] = Field(
        None,
        description=(
            "True if EMA-9/EMA-21 trend confirms direction. "
            "LONG: EMA-9 > EMA-21.  SHORT: EMA-9 < EMA-21. "
            "null when EMA data is unavailable."
        ),
    )

    # ── MACD ─────────────────────────────────────────────────────────────────
    macd_confirms: Optional[bool] = Field(
        None,
        description=(
            "True if MACD histogram confirms direction. "
            "LONG: histogram > 0.  SHORT: histogram < 0. "
            "null when MACD data is unavailable."
        ),
    )

    # ── Risk / Reward ─────────────────────────────────────────────────────────
    risk_reward_ratio: float = Field(
        description=(
            "|target − entry| / |entry − stop_loss|.  "
            "Must be ≥ 2.0 (hard minimum for NSE intraday MIS)."
        )
    )

    # ── Overall ───────────────────────────────────────────────────────────────
    confidence_score: int = Field(
        ge=0,
        le=100,
        description=(
            "Your overall confidence in this trade (0–100). "
            "< 45 → REJECT.  45–64 → REDUCE (half qty).  ≥ 65 → EXECUTE eligible."
        ),
    )
    conditions_not_met: List[str] = Field(
        default_factory=list,
        description=(
            "Exhaustive list of conditions that are NOT satisfied. Be truthful. "
            "Examples: 'rsi_overbought', 'volume_below_threshold', 'ema_not_aligned', "
            "'macd_against_direction', 'rr_insufficient', 'price_overextended_from_vwap', "
            "'market_bias_contradicts_signal'."
        ),
    )


class DecisionOutput(BaseModel):
    """
    Exact schema the Decision Engine LLM must return.
    Used for Pydantic validation of Claude's trade decision.
    """

    decision: Decision
    adjusted_qty: int = Field(ge=0)
    stop_loss_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    product_type: ProductType
    signal_audit: SignalAudit
    rationale: str = Field(
        max_length=300,
        description="Brief reason citing the signal_audit values that drove this decision.",
    )

