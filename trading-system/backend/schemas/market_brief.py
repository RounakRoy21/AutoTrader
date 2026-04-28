"""
Pydantic schemas for the Market Brief — used to validate LLM output
and for API request/response serialisation.
"""

from __future__ import annotations

from datetime import date, time
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


# ── Enums ──────────────────────────────────────────

class MarketBias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class SgxSignal(str, Enum):
    GAP_UP = "GAP_UP"
    GAP_DOWN = "GAP_DOWN"
    FLAT = "FLAT"


class FiiDiiSignal(str, Enum):
    LEAN_LONG = "LEAN_LONG"
    LEAN_SHORT = "LEAN_SHORT"
    NEUTRAL = "NEUTRAL"


class DxyTrend(str, Enum):
    STRENGTHENING = "STRENGTHENING"
    WEAKENING = "WEAKENING"
    FLAT = "FLAT"


class DxySignal(str, Enum):
    POSITIVE_FOR_EM = "POSITIVE_FOR_EM"
    NEGATIVE_FOR_EM = "NEGATIVE_FOR_EM"
    NEUTRAL = "NEUTRAL"


class UsMarketsSignal(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class NewsSentiment(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class NewsUrgency(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RecommendedStance(str, Enum):
    FULL_SIZE_POSITIONS = "FULL_SIZE_POSITIONS"
    HALF_SIZE_POSITIONS = "HALF_SIZE_POSITIONS"
    AVOID_TRADING = "AVOID_TRADING"


# ── Sub-schemas ────────────────────────────────────

class SgxNiftySchema(BaseModel):
    value: float
    change_pct: float
    signal: SgxSignal


class FiiDiiSchema(BaseModel):
    fii_net_crore: float
    dii_net_crore: float
    signal: FiiDiiSignal


class DxySchema(BaseModel):
    value: float
    trend: DxyTrend
    signal: DxySignal


class UsMarketsSchema(BaseModel):
    sp500_close_pct: float
    nasdaq_close_pct: float
    signal: UsMarketsSignal


class NewsFlagSchema(BaseModel):
    type: str
    sentiment: NewsSentiment
    urgency: NewsUrgency
    stock: Optional[str] = None
    beat_pct: Optional[float] = None
    headline: Optional[str] = None  # the actual news title that drove this flag


class EarningsDriftCandidate(BaseModel):
    stock: str
    beat_pct: Optional[float] = None  # null for pre-earnings candidates; non-null only when an actual beat% is known from news


# ── Main Market Brief Schema ──────────────────────

class MarketBriefLLMOutput(BaseModel):
    """
    Exact schema that the Research Agent LLM must return.
    Used for Pydantic validation of Claude's output.
    """
    date: str  # YYYY-MM-DD
    generated_at: str  # HH:MM:SS
    market_bias: MarketBias
    bias_confidence: float = Field(ge=0.0, le=1.0)
    sgx_nifty: SgxNiftySchema
    fii_dii: FiiDiiSchema
    dxy: DxySchema
    us_markets: UsMarketsSchema
    news_flags: List[NewsFlagSchema] = []
    watchlist_today: List[str] = []
    avoid_today: List[str] = []
    earnings_drift_candidates: List[EarningsDriftCandidate] = []
    recommended_stance: RecommendedStance
    position_size_override: Optional[str] = None


# ── API Response Schema ───────────────────────────

class MarketBriefResponse(BaseModel):
    """Schema for the GET /api/market-brief/today response body."""
    id: int
    date: date
    generated_at: time
    market_bias: str
    bias_confidence: float
    sgx_nifty_signal: Optional[str]
    fii_signal: Optional[str]
    dxy_signal: Optional[str]
    us_markets_signal: Optional[str]
    watchlist: Optional[list]
    avoid_list: Optional[list]
    recommended_stance: Optional[str]
    raw_json: dict

    class Config:
        from_attributes = True
