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
from core.redis_client import get_value, publish, set_value, delete_value
from core.redis_keys import LATEST_MARKET_BRIEF_KEY, TODAY_WATCHLIST_KEY, INSTRUMENT_MAP_KEY
from core.nse_calendar import is_nse_holiday
from integrations.alpha_vantage_client import (
    fetch_crude_oil,
    fetch_dxy,
    fetch_earnings_calendar,
    fetch_gold,
    fetch_india_vix,
    fetch_nikkei,
    fetch_sgx_nifty,
    fetch_us_market_close,
    fetch_usdinr,
)
from integrations.anthropic_client import get_anthropic_client
from integrations.news_aggregator import HybridNewsAggregator
from integrations.nse_client import fetch_corporate_actions_today, fetch_fii_dii_data, fetch_nse_indices
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
    NewsSentiment,
    NewsUrgency,
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
    "performance, S&P 500 overnight futures (ES=F), Nikkei 225 live performance, "
    "the Dollar Index (ICE DXY), USD/INR exchange rate, "
    "financial news headlines, and an earnings calendar. "
    "Your job is to synthesize this into "
    "a structured market brief. Be conservative in your bias scoring. Assign a "
    "BULLISH, BEARISH, or NEUTRAL market bias along with a confidence score between "
    "0.0 and 1.0. Identify which NIFTY 50 stocks to watch today and which to avoid. "
    "Flag any news that materially changes the risk profile. Return ONLY a valid JSON "
    "object — no explanation, no preamble, no markdown. "
    "The JSON must contain exactly these top-level keys: "
    "date, generated_at, market_bias, bias_confidence, sgx_nifty, fii_dii, dxy, "
    "us_markets, news_flags, watchlist_today, avoid_today, earnings_drift_candidates, "
    "recommended_stance, position_size_override. "
    "sgx_nifty is now sourced from S&P 500 futures (ES=F) — it represents the estimated "
    "Nifty 50 opening gap derived from overnight futures, not GIFT Nifty.\n"
    "news_flags is a list of objects with keys: type, sentiment (POSITIVE/NEGATIVE/NEUTRAL), "
    "urgency (HIGH/MEDIUM/LOW), stock (nullable), beat_pct (nullable — only non-null when a "
    "reported beat percentage is cited in the headline, e.g. 'beats estimates by 8%'), "
    "headline (the exact article title from the input that drove this flag — always populate this). "
    "earnings_drift_candidates is a list of objects with keys: stock, beat_pct (nullable). "
    "For stocks sourced from the earnings_calendar (upcoming results, not yet reported), "
    "beat_pct must be null. Only set beat_pct to a non-null float when a confirmed EPS beat "
    "percentage is explicitly cited in a news headline. "
    "recommended_stance must be one of: FULL_SIZE_POSITIONS, HALF_SIZE_POSITIONS, AVOID_TRADING. "
    "position_size_override is a nullable string; use null unless a specific override is warranted "
    "(e.g. 'REDUCE_50PCT' during VIX STRESS).\n\n"
    "NEWS HEADLINE INTERPRETATION RULES:\n"
    "  • Each headline has an 'age_minutes' field. Weight headlines under 180 minutes "
    "(3 hours) as HIGH relevance — these are fresh overnight catalysts not yet priced in. "
    "Headlines over 720 minutes (12 hours) are background context only — do not base "
    "watchlist decisions solely on them.\n"
    "  • Headlines with a 'stock_tag' field are the result of a targeted company search — "
    "treat them as confirmed to be about that stock. Headlines from RSS feeds without a "
    "stock_tag require your own attribution from the title.\n"
    "  • Prioritise source quality: economic_times and business_standard are primary "
    "sources; google_news aggregates and may include opinion pieces.\n"
    "  • A headline about a SEBI action, court order, promoter pledge, or block deal "
    "is higher urgency than a routine analyst target change.\n\n"
    "OVERNIGHT GAP AND ASIAN SESSION RULES:\n"
    "  • sgx_nifty is derived from S&P 500 futures (ES=F) overnight change × 0.65 "
    "(Nifty/SPX historical beta). It is the best available estimate of Nifty gap direction.\n"
    "  • sgx_nifty.signal=GAP_UP (est. Nifty >+0.2%): bullish lean, "
    "supports FULL_SIZE_POSITIONS if other signals agree.\n"
    "  • sgx_nifty.signal=GAP_DOWN (est. Nifty <-0.2%): bearish lean, "
    "gaps down >0.5% warrant HALF_SIZE_POSITIONS even on BULLISH bias.\n"
    "  • sgx_nifty.signal=FLAT: no directional edge from futures; "
    "rely on other signals.\n"
    "  • nikkei.signal is the live Nikkei 225 performance (Tokyo market open at 6 AM IST). "
    "It is an independent Asian risk signal with ~0.5 correlation to Nifty.\n"
    "  • nikkei.signal=NEGATIVE and sgx_nifty.signal=GAP_DOWN together: "
    "strong pan-Asian risk-off — lean BEARISH, recommend HALF_SIZE_POSITIONS.\n"
    "  • nikkei.signal=POSITIVE and sgx_nifty.signal=GAP_UP together: "
    "broad Asian risk-on, adds confidence to BULLISH bias.\n"
    "  • nikkei.available=False: Tokyo data unavailable — ignore the field.\n\n"
    "INDIA VIX INTERPRETATION RULES:\n"
    "  • india_vix.value is the NSE volatility index (30-day implied vol of Nifty options).\n"
    "  • regime=LOW (<14): complacency — momentum strategies work well, full-size positions "
    "appropriate.\n"
    "  • regime=NORMAL (14–20): standard environment — use normal position sizing and default "
    "stance.\n"
    "  • regime=ELEVATED (20–25): anxiety — compress bias_confidence by ~20%, recommend "
    "HALF_SIZE_POSITIONS even on BULLISH bias.\n"
    "  • regime=STRESS (>25): crisis — recommend AVOID_TRADING unless news catalyst is extremely "
    "clear; set position_size_override to 'REDUCE_50PCT' or higher.\n"
    "  • regime=UNKNOWN: VIX data unavailable — treat as NORMAL but note the gap.\n"
    "  • VIX regime overrides directional signals when they conflict: a BULLISH bias with "
    "regime=STRESS still warrants AVOID_TRADING or HALF_SIZE_POSITIONS.\n\n"
    "COMMODITY INTERPRETATION RULES:\n"
    "  • crude_oil.change_pct is WTI futures overnight % change. crude_oil.available=False "
    "means the fetch failed — ignore the field.\n"
    "  • Crude oil impact on NSE stocks:\n"
    "    - change_pct > +2%: BEARISH for downstream consumers — BPCL, HPCL, IOC (margin "
    "compression), IndiGo/aviation (fuel costs), Asian Paints/Pidilite (raw material costs). "
    "Add these to avoid_today unless a strong stock-specific catalyst overrides. BULLISH for "
    "upstream producers: ONGC, Oil India — consider adding to watchlist_today.\n"
    "    - change_pct < -2%: BULLISH signal for downstream consumers listed above; BEARISH "
    "for upstream producers.\n"
    "    - Absolute change between -2% and +2%: no material sector adjustment needed.\n"
    "  • gold.change_pct is gold futures overnight % change. gold.available=False means "
    "the fetch failed — ignore the field.\n"
    "  • Gold as a risk-off indicator:\n"
    "    - change_pct > +1% AND sgx_nifty is FLAT or GAP_DOWN: compress bias_confidence "
    "by 10–15% — risk-off sentiment is active, equity upside is capped.\n"
    "    - change_pct > +1% AND dxy is STRENGTHENING simultaneously: this is strong safe-haven "
    "demand (geopolitical stress pattern) — lean BEARISH or NEUTRAL regardless of US markets "
    "signal; recommend HALF_SIZE_POSITIONS.\n"
    "    - change_pct < -0.5%: risk-on signal, mildly supportive for equities.\n"
    "    - Jewellery stocks (TITAN): gold rally > +1% is modestly BULLISH for TITAN as "
    "investor interest in gold/senior jewellery rises; add to watchlist if no other negatives.\n\n"
    "NSE SECTOR INDICES INTERPRETATION RULES:\n"
    "  • nse_sector_indices contains live NSE index values: NIFTY BANK, NIFTY IT, NIFTY PHARMA, "
    "NIFTY AUTO, NIFTY FMCG, NIFTY METAL, NIFTY ENERGY, NIFTY REALTY, etc. "
    "Each entry has current, previous_close, and percent_change.\n"
    "  • Use this to distinguish broad market weakness from defensive rotation — two patterns "
    "that look identical at the Nifty 50 index level but require different trading responses:\n"
    "    - BROAD SELLOFF: 7+ sectors negative with cyclicals (BANK, IT, AUTO, METAL) leading "
    "the decline → high-conviction BEARISH, all sectors confirming weakness. "
    "Increase bias_confidence by 0.05–0.10 on a BEARISH call.\n"
    "    - DEFENSIVE ROTATION: cyclical sectors (BANK, IT, AUTO, METAL) down while defensive "
    "sectors (PHARMA, FMCG) are flat or positive → money is rotating, not fleeing. "
    "This is NEUTRAL, not BEARISH. Do NOT assign BEARISH bias solely on cyclical weakness "
    "when defensives are holding. Recommend HALF_SIZE_POSITIONS, favour defensive-sector stocks.\n"
    "    - BROAD RALLY: 7+ sectors positive → adds conviction to BULLISH bias.\n"
    "  • NIFTY BANK weight note: Financials are ~35% of Nifty 50. NIFTY BANK movement "
    "dominates the index. A 2%+ move in NIFTY BANK (either direction) is more significant "
    "than a 2%+ move in any other sector index — weight it accordingly.\n"
    "  • When nse_sector_indices is empty (NSE API unavailable or pre-open), ignore this section "
    "and rely on the other signals.\n\n"
    "DXY AND USD/INR INTERPRETATION RULES:\n"
    "  • dxy.value is the ICE US Dollar Index level (typically 95–110). "
    "dxy.trend: STRENGTHENING = USD gaining vs basket; WEAKENING = USD losing vs basket.\n"
    "  • usdinr.value is the USD/INR spot rate (e.g. 84.5 means 1 USD = ₹84.5). "
    "usdinr.available=False means the fetch failed — ignore the field.\n"
    "  • usdinr.trend: INR_WEAKENING = rupee depreciating (USD/INR rising), "
    "INR_STRENGTHENING = rupee appreciating, STABLE = no significant move.\n"
    "  • INR_WEAKENING is the most India-specific bearish signal: "
    "FIIs sell Indian equities to avoid currency losses on repatriation. "
    "Compress bias_confidence by 10% on INR_WEAKENING days.\n"
    "  • INR_STRENGTHENING is mildly bullish — supports FII inflows.\n"
    "  • Combined signal: dxy STRENGTHENING + INR_WEAKENING together = "
    "strong EM risk-off, lean BEARISH or recommend HALF_SIZE_POSITIONS.\n\n"
    "EARNINGS CALENDAR INTERPRETATION RULES:\n"
    "  • earnings_calendar contains stocks with scheduled NSE results in the next 7 calendar "
    "days: [{\"stock\": \"INFY\", \"earnings_date\": \"2026-03-12\"}, ...]. "
    "An empty list means no results are due this week.\n"
    "  • Results TODAY or TOMORROW: highest-uncertainty window. Pre-announcement drift is "
    "unpredictable. Add to watchlist_today only if macro and news context strongly support a "
    "positive surprise; otherwise include in avoid_today.\n"
    "  • Results in 3–7 days: moderate uncertainty. If overall bias is BULLISH and no negative "
    "stock-specific news exists, include in watchlist_today as a drift candidate.\n"
    "  • Populate earnings_drift_candidates for every stock in earnings_calendar. "
    "Set beat_pct to null — these are upcoming (not yet reported) results; actual beat "
    "percentages are unknown. Do not fabricate beat_pct values.\n"
    "  • If earnings_calendar is empty, return earnings_drift_candidates as [].\n"
    "  • earnings_calendar stocks with upcoming results near a VIX STRESS or ELEVATED regime "
    "should be treated as doubly uncertain — lean towards avoid_today.\n\n"
    "OUTPUT SIZE RULES:\n"
    "  • news_flags: return at most 15 items. Include only HIGH and MEDIUM urgency flags. "
    "Discard LOW urgency items entirely — they add no trading value.\n"
    "  • headline: truncate to 120 characters maximum. Do not pad or paraphrase — "
    "use the start of the actual title.\n"
    "  • type: use a short snake_case label, max 4 words (e.g. EARNINGS_BEAT, FII_SELLING, "
    "REGULATORY_ACTION, MACRO_DATA, SECTOR_NEWS). Never write a sentence."
)


async def collect_pre_market_data() -> dict:
    """
    Collect data from all external APIs in parallel.
    Returns a dict of raw data keyed by source.
    """
    logger.info("Starting pre-market data collection…")

    # Use yesterday's watchlist for targeted Google News per-stock queries.
    # The Research Agent stores its output watchlist in Redis each day, so by
    # 6 AM the previous session's list is already available \u2014 stocks flagged
    # yesterday remain relevant today (earnings clusters, sector moves, macro
    # events persist across sessions).  Falls back to the built-in 10-stock
    # default on first ever run (cold start).
    prior_watchlist: list[str] | None = None
    try:
        raw_brief = await get_value(LATEST_MARKET_BRIEF_KEY)
        if raw_brief:
            prior_watchlist = json.loads(raw_brief).get("watchlist_today")
    except Exception as _wl_exc:
        logger.debug("Could not load prior watchlist from Redis: %s", _wl_exc)

    if prior_watchlist:
        logger.info("fetch_all: using prior watchlist (%d symbols)", len(prior_watchlist))
    else:
        logger.info("fetch_all: no prior watchlist found \u2014 using default 10-stock set")

    _aggregator = HybridNewsAggregator()

    (
        fii_dii, us_markets, dxy, sgx_nifty, india_vix,
        crude_oil, gold, earnings_cal, news_items, corp_actions,
        nse_indices, usdinr, nikkei,
    ) = await asyncio.gather(
        fetch_fii_dii_data(),
        fetch_us_market_close(),
        fetch_dxy(),
        fetch_sgx_nifty(),
        fetch_india_vix(),
        fetch_crude_oil(),
        fetch_gold(),
        fetch_earnings_calendar(prior_watchlist),
        _aggregator.fetch_all(watchlist=prior_watchlist),
        fetch_corporate_actions_today(),
        fetch_nse_indices(),
        fetch_usdinr(),
        fetch_nikkei(),
        return_exceptions=True,
    )

    # ── NSE data override ────────────────────────────────────────────────────
    # NSE's own API is the authoritative source for India VIX (it's an NSE
    # product) and live Nifty 50 values.  Override the Yahoo Finance results
    # when NSE data is available and valid.  Yahoo Finance remains the fallback
    # if NSE returns an error or the markets are not yet open.
    if not isinstance(nse_indices, Exception) and nse_indices.get("available"):
        nse_vix = nse_indices.get("india_vix")
        nse_n50 = nse_indices.get("nifty50")

        if nse_vix and nse_vix.get("value", 0) > 0:
            india_vix = {"value": nse_vix["value"], "regime": nse_vix["regime"]}
            logger.info(
                "India VIX overridden from NSE: %.2f regime=%s",
                nse_vix["value"], nse_vix["regime"],
            )

        if nse_n50 and nse_n50.get("current", 0) > 0:
            pct = nse_n50["percent_change"]
            sgx_nifty = {
                "value": nse_n50["current"],
                "change_pct": pct,
                "signal": "GAP_UP" if pct > 0.2 else ("GAP_DOWN" if pct < -0.2 else "FLAT"),
            }
            logger.info(
                "Nifty50 overridden from NSE: %.2f (%.3f%%)",
                nse_n50["current"], pct,
            )

    # Handle any failures gracefully
    # mode='json' gives ISO-8601 datetimes; exclude 'link' because URLs
    # consume ~100 tokens each and Claude cannot act on them.
    if not isinstance(news_items, Exception):
        _all_news = sorted(
            [item.model_dump(mode="json", exclude={"link"}) for item in news_items],
            key=lambda x: x.get("age_minutes", 9999),  # most recent first
        )
        # Drop old generic-RSS background items (no stock_tag, older than 12 h).
        # Stock-tagged items (confirmed company-specific) are always kept.
        # Cap at 35 items: LLM returns max 15 flags, so 35 gives enough variety
        # without padding the prompt with low-signal noise.
        raw_news = [
            n for n in _all_news
            if n.get("stock_tag") or n.get("age_minutes", 9999) <= 720
        ][:35]
    else:
        raw_news = []

    # Detect total news blackout (network outage / IP block at 6 AM).
    # fetch_all() never raises — it returns [] on full failure — so we must
    # check explicitly.  Brief still runs but operator is alerted.
    if not raw_news:
        logger.warning(
            "All news sources returned 0 items — possible network outage or IP block. "
            "Brief will be generated from macro data only."
        )
        asyncio.create_task(publish("system_alerts", {
            "type": "warning",
            "message": "News aggregator returned 0 items from all sources. "
                       "Brief generated without news context.",
            "timestamp": datetime.now(IST).isoformat(),
        }))

    raw_data = {
        "fii_dii": fii_dii if not isinstance(fii_dii, Exception) else {"error": str(fii_dii)},
        "us_markets": us_markets if not isinstance(us_markets, Exception) else {"error": str(us_markets)},
        "dxy": dxy if not isinstance(dxy, Exception) else {"error": str(dxy)},
        # sgx_nifty key kept for LLM prompt compatibility; value is now sourced from
        # NSE allIndices (live Nifty50) when available, Yahoo Finance otherwise.
        "sgx_nifty": sgx_nifty if not isinstance(sgx_nifty, Exception) else {"error": str(sgx_nifty)},
        "india_vix": india_vix if not isinstance(india_vix, Exception) else {"value": 0.0, "regime": "UNKNOWN"},
        # sector_indices: included in raw_data so Claude receives live sector performance.
        # Interpretation rules are in RESEARCH_SYSTEM_PROMPT under
        # "NSE SECTOR INDICES INTERPRETATION RULES".
        "nse_sector_indices": (
            nse_indices.get("sector_indices", {})
            if not isinstance(nse_indices, Exception) and nse_indices.get("available")
            else {}
        ),
        "usdinr": usdinr if not isinstance(usdinr, Exception) else {"value": 0.0, "change_pct": 0.0, "trend": "STABLE", "available": False},
        "nikkei": nikkei if not isinstance(nikkei, Exception) else {"value": 0.0, "change_pct": 0.0, "signal": "FLAT", "available": False},
        "crude_oil": crude_oil if not isinstance(crude_oil, Exception) else {"price": 0.0, "change_pct": 0.0, "available": False},
        "gold": gold if not isinstance(gold, Exception) else {"price": 0.0, "change_pct": 0.0, "available": False},
        "news_headlines": raw_news,
        "earnings_calendar": earnings_cal if not isinstance(earnings_cal, Exception) else [],
        "corporate_actions_today": corp_actions if not isinstance(corp_actions, Exception) else [],
    }

    logger.info("Pre-market data collection complete")
    return raw_data


def _parse_news_flags(headlines: list) -> list[NewsFlagSchema]:
    """Convert HybridNewsAggregator headlines into NewsFlagSchema entries via keyword heuristics.

    Used by _generate_mock_brief so paper-trading sessions still reflect real
    news risk — something that was silently discarded when news_flags was
    hardcoded to [].  The heuristic is intentionally conservative: it only
    uses the article title and caps output at 10 items.
    """
    NEGATIVE_KEYWORDS = {
        "crash", "fall", "drop", "plunge", "decline", "loss", "losses", "fraud",
        "probe", "ban", "delist", "downgrade", "sell-off", "warning", "risk",
        "concern", "weak", "slowdown", "penalty", "default", "cut", "miss",
        "below expectations", "disappoints",
    }
    POSITIVE_KEYWORDS = {
        "rise", "gain", "rally", "surge", "profit", "upgrade", "dividend",
        "buyback", "strong", "beat", "record", "growth", "order win",
        "outperform", "beat estimates",
    }
    HIGH_URGENCY_KEYWORDS = {
        "fraud", "ban", "penalty", "default", "probe", "merger", "acquisition",
        "crash", "plunge", "emergency", "crisis", "rbi rate", "sebi", "halt",
        "circuit breaker",
    }
    # All NIFTY 50 constituents: symbol → lowercased search strings.
    # Ordered so that more specific phrases (e.g. "hdfc bank") are checked
    # before shorter tokens to avoid false partial matches.
    STOCK_KEYWORDS: dict[str, list[str]] = {
        # ── Financials ────────────────────────────────────────────────────────
        "HDFCBANK":   ["hdfc bank", "hdfcbank"],
        "ICICIBANK":  ["icici bank", "icicibank"],
        "KOTAKBANK":  ["kotak bank", "kotak mahindra bank"],
        "SBIN":       ["state bank of india", "state bank", "sbi "],
        "AXISBANK":   ["axis bank", "axisbank"],
        "BAJFINANCE": ["bajaj finance"],
        "BAJAJFINSV": ["bajaj finserv"],
        "INDUSINDBK": ["indusind bank"],
        "HDFCLIFE":   ["hdfc life"],
        "SBILIFE":    ["sbi life"],
        # ── Information Technology ────────────────────────────────────────────
        "INFY":       ["infosys"],
        "TCS":        ["tata consultancy", " tcs "],
        "WIPRO":      ["wipro"],
        "HCLTECH":    ["hcl tech", "hcl technologies"],
        "TECHM":      ["tech mahindra"],
        "LTIM":       ["ltimindtree", "lti mindtree"],
        # ── Consumer / FMCG ──────────────────────────────────────────────────
        "RELIANCE":   ["reliance industries", "reliance jio", "reliance retail", "reliance"],
        "HINDUNILVR": ["hindustan unilever", " hul "],
        "NESTLEIND":  ["nestle india"],
        "ITC":        ["itc limited", "itc ltd", " itc "],
        "BRITANNIA":  ["britannia"],
        "TATACONSUM": ["tata consumer"],
        "DABUR":      ["dabur"],
        # ── Automobiles ──────────────────────────────────────────────────────
        "MARUTI":     ["maruti suzuki", "maruti"],
        "TATAMOTORS": ["tata motors"],
        "M&M":        ["mahindra & mahindra", "mahindra and mahindra"],
        "BAJAJ-AUTO": ["bajaj auto"],
        "HEROMOTOCO": ["hero motocorp", "hero moto"],
        "EICHERMOT":  ["eicher motors", "royal enfield"],
        # ── Metals & Mining ───────────────────────────────────────────────────
        "TATASTEEL":  ["tata steel"],
        "JSWSTEEL":   ["jsw steel"],
        "HINDALCO":   ["hindalco"],
        "COALINDIA":  ["coal india"],
        "VEDL":       ["vedanta"],
        # ── Energy & Utilities ────────────────────────────────────────────────
        "ONGC":       ["oil and natural gas corporation", "ongc"],
        "BPCL":       ["bharat petroleum", "bpcl"],
        "NTPC":       ["ntpc"],
        "POWERGRID":  ["power grid corporation", "powergrid"],
        # ── Pharmaceuticals ───────────────────────────────────────────────────
        "SUNPHARMA":  ["sun pharma", "sun pharmaceutical"],
        "DRREDDY":    ["dr. reddy", "dr reddy"],
        "CIPLA":      ["cipla"],
        "DIVISLAB":   ["divi's lab", "divi laboratories"],
        # ── Cement & Construction ─────────────────────────────────────────────
        "ULTRACEMCO": ["ultratech cement"],
        "GRASIM":     ["grasim"],
        "LT":         ["larsen & toubro", "larsen and toubro", "l&t"],
        # ── Diversified / Others ─────────────────────────────────────────────
        "TITAN":      ["titan company"],
        "ASIANPAINT": ["asian paints"],
        "PIDILITIND": ["pidilite"],
        "APOLLOHOSP": ["apollo hospitals"],
        "BHARTIARTL": ["bharti airtel", "airtel"],
        "ADANIPORTS": ["adani ports"],
        "ADANIENT":   ["adani enterprises", "adani group"],
    }

    flags: list[NewsFlagSchema] = []
    for article in headlines:  # process all fetched articles
        title = (article.get("title") or "").lower()
        if not title:
            continue

        neg_score = sum(1 for kw in NEGATIVE_KEYWORDS if kw in title)
        pos_score = sum(1 for kw in POSITIVE_KEYWORDS if kw in title)
        if neg_score > pos_score:
            sentiment = NewsSentiment.NEGATIVE
        elif pos_score > neg_score:
            sentiment = NewsSentiment.POSITIVE
        else:
            sentiment = NewsSentiment.NEUTRAL

        has_high = any(kw in title for kw in HIGH_URGENCY_KEYWORDS)
        urgency = NewsUrgency.HIGH if has_high else (
            NewsUrgency.MEDIUM if neg_score + pos_score >= 2 else NewsUrgency.LOW
        )

        # Prefer the explicit stock_tag set by the HybridNewsAggregator for
        # Google News per-stock queries.  Fall back to keyword matching for
        # RSS feed articles that have no tag.
        matched_stock: str | None = article.get("stock_tag")
        if matched_stock is None:
            for symbol, names in STOCK_KEYWORDS.items():
                if any(name in title for name in names):
                    matched_stock = symbol
                    break

        # Skip generic low-signal articles (no stock match + LOW urgency = noise)
        if matched_stock is None and urgency == NewsUrgency.LOW:
            continue

        flags.append(NewsFlagSchema(
            type="NEWS",
            sentiment=sentiment,
            urgency=urgency,
            stock=matched_stock,
            headline=article.get("title"),
        ))

        if len(flags) >= 15:  # cap output so the brief stays concise
            break

    return flags


_FALLBACK_WATCHLIST = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK", "AXISBANK", "WIPRO"]


def _load_prior_watchlist() -> list[str]:
    """Synchronous shim — always returns the fallback list.

    The real async version is _async_load_prior_watchlist(), called from
    generate_market_brief() before _generate_mock_brief() is invoked.
    This stub is kept so any external import (e.g. the Docker import check)
    doesn't break.
    """
    return _FALLBACK_WATCHLIST


async def _async_load_prior_watchlist() -> list[str]:
    """Async-safe: reads TODAY_WATCHLIST_KEY from Redis, falls back to FALLBACK."""
    try:
        raw = await get_value(TODAY_WATCHLIST_KEY)
        if raw:
            wl = json.loads(raw)
            if isinstance(wl, list) and wl:
                logger.debug("Mock brief using prior watchlist from Redis: %s", wl)
                return wl
    except Exception as exc:
        logger.debug("Could not load prior watchlist for mock brief: %s", exc)
    return _FALLBACK_WATCHLIST


def _generate_mock_brief(
    raw_data: dict | None = None,
    prior_watchlist: list[str] | None = None,
) -> MarketBriefLLMOutput:
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

    crude_change = 0.0
    gold_change = 0.0
    if raw_data:
        crude = raw_data.get("crude_oil", {})
        if crude.get("available"):
            crude_change = crude.get("change_pct", 0.0)
        gold = raw_data.get("gold", {})
        if gold.get("available"):
            gold_change = gold.get("change_pct", 0.0)

    # Derive simple bias from available data.
    # Gold > +1% is a risk-off bear signal; crude > +2% is mildly bearish overall
    # (net negative for the broader market even if bullish for specific upstream names).
    bull_count = sum([
        sp500_pct > 0.2,
        nasdaq_pct > 0.2,
        fii_net > 200,
        sgx_change > 0.2,
        gold_change < -0.5,    # risk-on: gold falling
    ])
    bear_count = sum([
        sp500_pct < -0.2,
        nasdaq_pct < -0.2,
        fii_net < -200,
        sgx_change < -0.2,
        gold_change > 1.0,     # risk-off: gold rallying
        crude_change > 2.0,    # cost-push pressure on broad market
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

    # Apply India VIX regime to stance and confidence — mirrors the LLM instructions.
    # Paper mode uses the same real-time VIX data fetched during collect_pre_market_data().
    vix_regime = "NORMAL"
    if raw_data:
        vix_regime = raw_data.get("india_vix", {}).get("regime", "NORMAL") or "NORMAL"
    if vix_regime == "STRESS":
        stance = RecommendedStance.AVOID_TRADING
        confidence = round(confidence * 0.6, 2)
    elif vix_regime == "ELEVATED":
        if stance == RecommendedStance.FULL_SIZE_POSITIONS:
            stance = RecommendedStance.HALF_SIZE_POSITIONS
        confidence = round(confidence * 0.8, 2)

    # Paper mode: never block all signals with AVOID_TRADING.
    # The mock brief derives stance from real macro data (actual FII, actual VIX,
    # actual S&P 500) — on a genuinely bearish day this legitimately produces
    # AVOID_TRADING, but that blocks every signal via _pre_check and leaves paper
    # sessions with zero trades, making it impossible to verify the execution pipeline.
    # Cap at HALF_SIZE_POSITIONS so risk management still applies (half qty, signal
    # audit still runs) without completely silencing all paper signals.
    if get_settings().paper_trading and stance == RecommendedStance.AVOID_TRADING:
        logger.info(
            "[MOCK] Paper mode: macro analysis produced AVOID_TRADING "
            "— demoted to HALF_SIZE_POSITIONS so paper trades can fire"
        )
        stance = RecommendedStance.HALF_SIZE_POSITIONS

    # Parse real news headlines into structured flags (paper mode still sees real news)
    news_headlines = (raw_data.get("news_headlines") or []) if raw_data else []
    news_flags = _parse_news_flags(news_headlines)

    # Build a dynamic watchlist from today's real news signals so the list
    # actually changes day-to-day instead of cycling the same fallback forever.
    #
    # Priority: positive-news stocks → earnings calendar → prior/fallback fill-in.
    # Stocks with high/medium urgency negative news are excluded from the watchlist
    # and moved to avoid_today.
    _positive_from_news: list[str] = []
    _avoid_from_news: set[str] = set()
    for _flag in news_flags:
        if _flag.stock:
            if (
                _flag.sentiment == NewsSentiment.NEGATIVE
                and _flag.urgency in (NewsUrgency.HIGH, NewsUrgency.MEDIUM)
            ):
                _avoid_from_news.add(_flag.stock)
            elif (
                _flag.sentiment == NewsSentiment.POSITIVE
                and _flag.stock not in _positive_from_news
                and _flag.stock not in _avoid_from_news
            ):
                _positive_from_news.append(_flag.stock)

    # Earnings calendar stocks → potential drift candidates (skip on bearish days)
    _earnings_stocks: list[str] = []
    if raw_data and bias != MarketBias.BEARISH:
        for _e in (raw_data.get("earnings_calendar") or []):
            _s = _e.get("stock")
            if _s and _s not in _positive_from_news and _s not in _avoid_from_news:
                _earnings_stocks.append(_s)

    # Fill remaining slots from prior/fallback, skipping anything flagged negatively
    _base_list = prior_watchlist if prior_watchlist else _FALLBACK_WATCHLIST
    _fill = [s for s in _base_list if s not in _positive_from_news and s not in _avoid_from_news]

    _combined = _positive_from_news + _earnings_stocks + _fill
    watchlist_today = _combined[:10] if _combined else _FALLBACK_WATCHLIST
    avoid_today = sorted(_avoid_from_news)

    logger.info(
        "[MOCK] Generated mock brief: bias=%s confidence=%.2f vix_regime=%s stance=%s "
        "watchlist=%s (+%d from news, +%d earnings) avoid=%s",
        bias, confidence, vix_regime, stance.value,
        watchlist_today, len(_positive_from_news), len(_earnings_stocks), avoid_today,
    )
    return MarketBriefLLMOutput(
        date=now_ist.strftime("%Y-%m-%d"),
        generated_at=now_ist.strftime("%H:%M:%S"),
        market_bias=bias,
        bias_confidence=confidence,
        sgx_nifty=SgxNiftySchema(value=sgx_value or 22500.0, change_pct=sgx_change, signal=sgx_signal),
        fii_dii=FiiDiiSchema(fii_net_crore=fii_net, dii_net_crore=dii_net, signal=fii_sig),
        dxy=DxySchema(value=dxy_value, trend=dxy_trend, signal=dxy_sig),
        us_markets=UsMarketsSchema(sp500_close_pct=sp500_pct, nasdaq_close_pct=nasdaq_pct, signal=us_sig),
        news_flags=news_flags,
        watchlist_today=watchlist_today,
        avoid_today=avoid_today,
        earnings_drift_candidates=[
            EarningsDriftCandidate(stock=e["stock"], beat_pct=None)
            for e in (raw_data.get("earnings_calendar") or []) if raw_data
        ],
        recommended_stance=stance,
        position_size_override=None,
    )


def _is_placeholder_key(key: str) -> bool:
    """Return True if the API key looks like a placeholder value."""
    placeholders = {"placeholder", "your-key-here", "xxxx", "sk-ant-api03-placeholder"}
    return not key or any(p in key.lower() for p in placeholders)


async def generate_market_brief(raw_data: dict) -> MarketBriefLLMOutput | None:
    """
    Send collected data to Claude and validate the response.
    In paper trading mode or when the API key is a placeholder, returns a synthetic brief.
    Returns the parsed MarketBriefLLMOutput or None if the LLM fails after all retries.
    """
    settings = get_settings()

    # ── Missing/placeholder key → skip LLM, use mock brief ──
    # Paper trading mode still makes real LLM calls — only trade *execution* is mocked.
    if _is_placeholder_key(settings.anthropic_api_key):
        logger.info("Skipping LLM call (placeholder anthropic key) — returning mock brief")
        prior_wl = await _async_load_prior_watchlist()
        return _generate_mock_brief(raw_data, prior_watchlist=prior_wl)

    # ── Live mode: call Claude ──
    now_ist = datetime.now(IST)
    user_content = (
        f"Today is {now_ist.strftime('%Y-%m-%d')} ({now_ist.strftime('%A')}). "
        f"Current time: {now_ist.strftime('%H:%M:%S')} IST.\n\n"
        # separators=(',',':') produces compact JSON with no whitespace — saves ~25-30%
        # of tokens vs indent=2 on a typical ~8 k-token data payload.
        f"RAW DATA:\n{json.dumps(raw_data, separators=(',', ':'), default=str)}\n\n"
        "Generate the market brief JSON."
    )

    client = get_anthropic_client()
    brief = await client.generate_structured(
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        user_content=user_content,
        response_model=MarketBriefLLMOutput,
        max_tokens=6000,   # Sonnet on the research prompt needs ~3000–5000 tokens;
                           # 4096 (the default) causes truncation → retry → double call.
    )
    if brief is None:
        logger.warning("LLM failed — falling back to mock brief")
        prior_wl = await _async_load_prior_watchlist()
        return _generate_mock_brief(raw_data, prior_watchlist=prior_wl)
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

    # Persist today's watchlist so load_instrument_map() picks it up when
    # the trading session starts at 09:15 — the scanner then subscribes to
    # exactly the stocks the LLM flagged rather than the static focus_stocks.
    await set_value(TODAY_WATCHLIST_KEY, json.dumps(brief.watchlist_today), ttl=86400)
    # Invalidate the cached token map so the next load_instrument_map() call
    # re-fetches from Groww using the new watchlist.
    await delete_value(INSTRUMENT_MAP_KEY)
    # Eagerly refresh the in-memory map now (before 09:15) so a mid-day
    # manual restart also gets the correct stocks.
    try:
        from integrations.instrument_service import load_instrument_map  # lazy import
        await load_instrument_map()
        logger.info(
            "Instrument map refreshed with today's watchlist: %s", brief.watchlist_today
        )
    except Exception as _exc:
        logger.warning("Could not refresh instrument map after brief: %s", _exc)


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

    # Fire-and-forget feed health check — runs concurrently with data collection
    # so the 6 AM data fetch is never delayed by feed probing.  Failures are
    # logged and published to system_alerts by check_feed_health() itself.
    asyncio.create_task(HybridNewsAggregator().check_feed_health())

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

        # A4: Mechanical corporate-action override — force any ex-date stock into
        # avoid_today regardless of what the LLM decided.  Ex-date stocks have
        # their open adjusted by the exchange; VWAP, RSI and volume signals are
        # all distorted for the entire session and must not be traded.
        ex_date_stocks: list[str] = raw_data.get("corporate_actions_today", [])
        if ex_date_stocks:
            updated_avoid = list(dict.fromkeys(brief.avoid_today + ex_date_stocks))
            brief = brief.model_copy(update={"avoid_today": updated_avoid})
            logger.info(
                "Corporate actions: %d ex-date stock(s) forced into avoid_today: %s",
                len(ex_date_stocks), ex_date_stocks,
            )

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
