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
    rsi_cited: float = 0.0          # overwritten by _validate_decision()
    rsi_in_range: bool = False      # overwritten by _validate_decision()

    # ── Volume ───────────────────────────────────────────────────────────────
    volume_ratio_cited: float = 0.0  # overwritten by _validate_decision()
    volume_confirms: bool = False    # overwritten by _validate_decision()

    # ── VWAP ─────────────────────────────────────────────────────────────────
    vwap_deviation_pct: float = 0.0  # overwritten by _validate_decision()
    price_vwap_valid: bool = False   # overwritten by _validate_decision()

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
    risk_reward_ratio: Optional[float] = Field(
        None,
        description=(
            "|target − entry| / |entry − stop_loss|.  "
            "Must be ≥ 2.0 (hard minimum for NSE intraday MIS)."
        )
    )

    # ── Overall ───────────────────────────────────────────────────────────────
    # confidence_score is the only field TRUSTED from the LLM verbatim.
    # Default 70 (neutral) is used when the LLM omits the field.
    confidence_score: int = Field(
        default=70,
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

    stop_loss_price / target_price default to 0.0 — _validate_decision() always
    overwrites them from the raw signal, so omitting them from the LLM response
    is acceptable.  product_type is always MIS for NSE intraday.
    signal_audit sub-fields are mostly overwritten; only confidence_score and
    rationale carry genuine LLM judgment.
    """

    decision: Decision
    adjusted_qty: int = Field(default=0, ge=0)
    # Pre-computed in the prompt and overwritten in _validate_decision(); default
    # allows the LLM to omit them without causing a validation failure.
    stop_loss_price: float = 0.0
    target_price: float = 0.0
    product_type: ProductType = ProductType.MIS
    signal_audit: SignalAudit = Field(default_factory=SignalAudit)
    rationale: str = Field(
        max_length=500,
        description="One-sentence reason citing which thresholds passed/failed.",
    )

