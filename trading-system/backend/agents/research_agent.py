"""
Research Agent — Pre-market data collection and AI-powered market brief generation.

Runs from 6:00 AM to 9:10 AM IST every trading day.
Collects data from multiple external APIs, synthesises it via Claude,
and publishes a structured Market Brief to Redis + PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import pytz
from sqlalchemy import select

from core.config import get_settings
from core.database import get_db_context
from core.redis_client import publish, set_value
from core.redis_keys import LATEST_MARKET_BRIEF_KEY
from core.nse_calendar import is_nse_holiday
from integrations.alpha_vantage_client import (
    fetch_dxy,
    fetch_sgx_nifty,
    fetch_us_market_close,
)
from integrations.anthropic_client import get_anthropic_client
from integrations.news_client import fetch_market_news
from integrations.nse_client import fetch_fii_dii_data
from models.market_brief import MarketBrief
from schemas.market_brief import (
    DxySchema,
    DxySignal,
    DxyTrend,
    EarningsDriftCandidate,
    FiiDiiSchema,
    FiiDiiSignal,
    MarketBias,
    MarketBriefLLMOutput,
    NewsFlagSchema,
    RecommendedStance,
    SgxNiftySchema,
    SgxSignal,
    UsMarketsSchema,
    UsMarketsSignal,
)

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

RESEARCH_SYSTEM_PROMPT = (
    "You are a pre-market analyst for Indian equity markets with 20 years of "
    "experience. You will be given raw data including FII/DII activity, US market "
    "performance, the Dollar Index trend, SGX NIFTY futures direction, financial "
    "news headlines, and an earnings calendar. Your job is to synthesize this into "
    "a structured market brief. Be conservative in your bias scoring. Assign a "
    "BULLISH, BEARISH, or NEUTRAL market bias along with a confidence score between "
    "0.0 and 1.0. Identify which NIFTY 50 stocks to watch today and which to avoid. "
    "Flag any news that materially changes the risk profile. Return ONLY a valid JSON "
    "object — no explanation, no preamble, no markdown."
)


async def collect_pre_market_data() -> dict:
    """
    Collect data from all external APIs in parallel.
    Returns a dict of raw data keyed by source.
    """
    logger.info("Starting pre-market data collection…")

    fii_dii, us_markets, dxy, sgx_nifty, news = await asyncio.gather(
        fetch_fii_dii_data(),
        fetch_us_market_close(),
        fetch_dxy(),
        fetch_sgx_nifty(),
        fetch_market_news(),
        return_exceptions=True,
    )

    # Handle any failures gracefully
    raw_data = {
        "fii_dii": fii_dii if not isinstance(fii_dii, Exception) else {"error": str(fii_dii)},
        "us_markets": us_markets if not isinstance(us_markets, Exception) else {"error": str(us_markets)},
        "dxy": dxy if not isinstance(dxy, Exception) else {"error": str(dxy)},
        "sgx_nifty": sgx_nifty if not isinstance(sgx_nifty, Exception) else {"error": str(sgx_nifty)},
        "news_headlines": news if not isinstance(news, Exception) else [],
        "earnings_calendar": [],  # TODO: integrate Tickertape API
    }

    logger.info("Pre-market data collection complete")
    return raw_data


def _generate_mock_brief(raw_data: dict | None = None) -> MarketBriefLLMOutput:
    """
    Return a synthetic market brief for paper trading / testing without LLM.
    Incorporates any real data already collected (FII/DII, US markets, etc.).
    """
    now_ist = datetime.now(IST)

    # Lift real values if available
    fii_net = 0.0
    dii_net = 0.0
    sp500_pct = 0.0
    nasdaq_pct = 0.0
    dxy_value = 104.0
    sgx_value = 22500.0
    sgx_change = 0.0
    sgx_signal = SgxSignal.FLAT

    if raw_data:
        fii_net = raw_data.get("fii_dii", {}).get("fii_net_crore", 0.0)
        dii_net = raw_data.get("fii_dii", {}).get("dii_net_crore", 0.0)
        sp500_pct = raw_data.get("us_markets", {}).get("sp500_close_pct", 0.0)
        nasdaq_pct = raw_data.get("us_markets", {}).get("nasdaq_close_pct", 0.0)
        dxy_value = raw_data.get("dxy", {}).get("value", 104.0)
        sgx_value = raw_data.get("sgx_nifty", {}).get("value", 22500.0)
        sgx_change = raw_data.get("sgx_nifty", {}).get("change_pct", 0.0)
        raw_sgx_sig = raw_data.get("sgx_nifty", {}).get("signal", "FLAT")
        sgx_signal = SgxSignal(raw_sgx_sig) if raw_sgx_sig in SgxSignal.__members__ else SgxSignal.FLAT

    # Derive simple bias from available data
    bull_count = sum([
        sp500_pct > 0.2,
        nasdaq_pct > 0.2,
        fii_net > 200,
        sgx_change > 0.2,
    ])
    bear_count = sum([
        sp500_pct < -0.2,
        nasdaq_pct < -0.2,
        fii_net < -200,
        sgx_change < -0.2,
    ])
    if bull_count >= 3:
        bias = MarketBias.BULLISH
        stance = RecommendedStance.FULL_SIZE_POSITIONS
        confidence = 0.65
    elif bear_count >= 3:
        bias = MarketBias.BEARISH
        stance = RecommendedStance.AVOID_TRADING
        confidence = 0.65
    else:
        bias = MarketBias.NEUTRAL
        stance = RecommendedStance.HALF_SIZE_POSITIONS
        confidence = 0.50

    # Derive DXY signal
    dxy_trend_val = raw_data.get("dxy", {}).get("trend", "FLAT") if raw_data else "FLAT"
    dxy_trend = DxyTrend(dxy_trend_val) if dxy_trend_val in DxyTrend.__members__ else DxyTrend.FLAT
    if dxy_trend == DxyTrend.WEAKENING:
        dxy_sig = DxySignal.POSITIVE_FOR_EM
    elif dxy_trend == DxyTrend.STRENGTHENING:
        dxy_sig = DxySignal.NEGATIVE_FOR_EM
    else:
        dxy_sig = DxySignal.NEUTRAL

    # FII signal
    if fii_net > 300 and dii_net > 0:
        fii_sig = FiiDiiSignal.LEAN_LONG
    elif fii_net < -300:
        fii_sig = FiiDiiSignal.LEAN_SHORT
    else:
        fii_sig = FiiDiiSignal.NEUTRAL

    # US signal
    if sp500_pct > 0.3 and nasdaq_pct > 0.3:
        us_sig = UsMarketsSignal.POSITIVE
    elif sp500_pct < -0.3 or nasdaq_pct < -0.3:
        us_sig = UsMarketsSignal.NEGATIVE
    else:
        us_sig = UsMarketsSignal.NEUTRAL

    logger.info("[MOCK] Generated mock brief: bias=%s confidence=%.2f", bias, confidence)
    return MarketBriefLLMOutput(
        date=now_ist.strftime("%Y-%m-%d"),
        generated_at=now_ist.strftime("%H:%M:%S"),
        market_bias=bias,
        bias_confidence=confidence,
        sgx_nifty=SgxNiftySchema(value=sgx_value or 22500.0, change_pct=sgx_change, signal=sgx_signal),
        fii_dii=FiiDiiSchema(fii_net_crore=fii_net, dii_net_crore=dii_net, signal=fii_sig),
        dxy=DxySchema(value=dxy_value, trend=dxy_trend, signal=dxy_sig),
        us_markets=UsMarketsSchema(sp500_close_pct=sp500_pct, nasdaq_close_pct=nasdaq_pct, signal=us_sig),
        news_flags=[],
        watchlist_today=["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK", "AXISBANK", "WIPRO"],
        avoid_today=[],
        earnings_drift_candidates=[],
        recommended_stance=stance,
        position_size_override=None,
    )


def _is_placeholder_key(key: str) -> bool:
    """Return True if the API key looks like a placeholder value."""
    placeholders = {"", "placeholder", "your-key-here", "xxxx", "sk-ant-api03-placeholder"}
    return not key or any(p in key.lower() for p in placeholders)


async def generate_market_brief(raw_data: dict) -> MarketBriefLLMOutput | None:
    """
    Send collected data to Claude and validate the response.
    In paper trading mode or when the API key is a placeholder, returns a synthetic brief.
    Returns the parsed MarketBriefLLMOutput or None if the LLM fails after all retries.
    """
    settings = get_settings()

    # ── Paper trading / missing key → skip LLM, use mock brief ──
    if settings.paper_trading or _is_placeholder_key(settings.anthropic_api_key):
        logger.info(
            "Skipping LLM call (paper_trading=%s, placeholder_key=%s) — returning mock brief",
            settings.paper_trading,
            _is_placeholder_key(settings.anthropic_api_key),
        )
        return _generate_mock_brief(raw_data)

    # ── Live mode: call Claude ──
    now_ist = datetime.now(IST)
    user_content = (
        f"Today is {now_ist.strftime('%Y-%m-%d')} ({now_ist.strftime('%A')}). "
        f"Current time: {now_ist.strftime('%H:%M:%S')} IST.\n\n"
        f"RAW DATA:\n{json.dumps(raw_data, indent=2, default=str)}\n\n"
        "Generate the market brief JSON."
    )

    client = get_anthropic_client()
    brief = await client.generate_structured(
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        user_content=user_content,
        response_model=MarketBriefLLMOutput,
    )
    if brief is None:
        logger.warning("LLM failed — falling back to mock brief")
        return _generate_mock_brief(raw_data)
    return brief


async def persist_and_publish(brief: MarketBriefLLMOutput) -> None:
    """Save the Market Brief to PostgreSQL and publish it to Redis.

    Uses an upsert pattern: if a brief for today's date already exists
    (e.g. manual trigger after the scheduler), the existing row is
    updated rather than inserting a duplicate.
    """
    brief_dict = brief.model_dump()
    brief_date = datetime.strptime(brief.date, "%Y-%m-%d").date()

    # Persist to PostgreSQL — upsert
    async with get_db_context() as session:
        existing = await session.execute(
            select(MarketBrief).where(MarketBrief.date == brief_date)
        )
        db_brief = existing.scalars().first()
        if db_brief is not None:
            # Update existing row
            db_brief.generated_at = datetime.strptime(brief.generated_at, "%H:%M:%S").time()
            db_brief.market_bias = brief.market_bias.value
            db_brief.bias_confidence = brief.bias_confidence
            db_brief.sgx_nifty_signal = brief.sgx_nifty.signal.value
            db_brief.fii_signal = brief.fii_dii.signal.value
            db_brief.dxy_signal = brief.dxy.signal.value
            db_brief.us_markets_signal = brief.us_markets.signal.value
            db_brief.watchlist = brief.watchlist_today
            db_brief.avoid_list = brief.avoid_today
            db_brief.recommended_stance = brief.recommended_stance.value
            db_brief.raw_json = brief_dict
            session.add(db_brief)
            logger.info("Market brief updated (upsert) for %s", brief.date)
        else:
            db_brief = MarketBrief(
                date=brief_date,
                generated_at=datetime.strptime(brief.generated_at, "%H:%M:%S").time(),
                market_bias=brief.market_bias.value,
                bias_confidence=brief.bias_confidence,
                sgx_nifty_signal=brief.sgx_nifty.signal.value,
                fii_signal=brief.fii_dii.signal.value,
                dxy_signal=brief.dxy.signal.value,
                us_markets_signal=brief.us_markets.signal.value,
                watchlist=brief.watchlist_today,
                avoid_list=brief.avoid_today,
                recommended_stance=brief.recommended_stance.value,
                raw_json=brief_dict,
            )
            session.add(db_brief)
            logger.info("Market brief persisted to PostgreSQL for %s", brief.date)

    # Publish to Redis
    await publish("market_brief", brief_dict)
    logger.info("Market brief published to Redis channel 'market_brief'")

    # Store the latest brief in a Redis key for easy access
    await set_value(LATEST_MARKET_BRIEF_KEY, json.dumps(brief_dict))


async def run_research_agent() -> None:
    """
    Main entry point for the Research Agent.
    Called by APScheduler at 6:00 AM IST, or triggered manually via the API.
    """
    if is_nse_holiday():
        logger.info("Research Agent: today is an NSE holiday — skipping run")
        return

    logger.info("═══ Research Agent starting ═══")
    await set_value("agent:research:status", "ACTIVE")
    await set_value("agent:research:last_run_started", datetime.now(IST).isoformat())
    await publish("system_alerts", {
        "type": "info",
        "message": "Research Agent started pre-market data collection",
        "timestamp": datetime.now(IST).isoformat(),
    })

    try:
        # Step 1: Collect data
        await set_value("agent:research:step", "COLLECTING")
        raw_data = await collect_pre_market_data()

        # Step 2: Generate brief
        await set_value("agent:research:step", "GENERATING")
        brief = await generate_market_brief(raw_data)

        if brief is None:
            logger.error("Research Agent failed to generate a valid market brief")
            await set_value("agent:research:status", "ERROR")
            await publish("system_alerts", {
                "type": "error",
                "message": "Research Agent failed to generate market brief",
                "timestamp": datetime.now(IST).isoformat(),
            })
            return

        # Step 3: Persist and broadcast
        await set_value("agent:research:step", "PERSISTING")
        await persist_and_publish(brief)

        await set_value("agent:research:last_run_completed", datetime.now(IST).isoformat())
        await set_value("agent:research:last_bias", brief.market_bias.value)
        await set_value("agent:research:last_confidence", str(brief.bias_confidence))
        await publish("system_alerts", {
            "type": "success",
            "message": (
                f"Market brief generated: {brief.market_bias.value} "
                f"(confidence {brief.bias_confidence:.0%}) — "
                f"watchlist {brief.watchlist_today}"
            ),
            "timestamp": datetime.now(IST).isoformat(),
        })
        logger.info(
            "═══ Research Agent complete — bias=%s confidence=%.2f ═══",
            brief.market_bias.value, brief.bias_confidence,
        )
    except Exception as exc:
        logger.exception("Research Agent encountered an error: %s", exc)
        await set_value("agent:research:status", "ERROR")
        await publish("system_alerts", {
            "type": "error",
            "message": f"Research Agent error: {exc}",
            "timestamp": datetime.now(IST).isoformat(),
        })
    finally:
        await set_value("agent:research:status", "INACTIVE")
        await set_value("agent:research:step", "IDLE")
