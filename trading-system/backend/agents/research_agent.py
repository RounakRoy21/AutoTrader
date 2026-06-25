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
from integrations.nse_client import (
    fetch_bulk_deals,
    fetch_corporate_actions_today,
    fetch_corporate_announcements,
    fetch_delivery_data,
    fetch_event_calendar,
    fetch_fii_dii_data,
    fetch_gift_nifty,
    fetch_nse_indices,
)
from integrations.rbi_client import fetch_rbi_updates
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
    "You are a pre-market analyst for Indian equity markets. Synthesise the RAW DATA into a "
    "structured market brief. Apply the BIAS_CONFIDENCE CALIBRATION section for all scoring. "
    "Return ONLY valid JSON — no preamble, no markdown. "
    "Global rule: for any field where available=False or the list/object is empty, skip that "
    "section and rely on the remaining signals.\n\n"

    "REQUIRED JSON KEYS: date, generated_at, market_bias (BULLISH|BEARISH|NEUTRAL), "
    "bias_confidence (0.0–1.0), sgx_nifty, fii_dii, dxy, us_markets, news_flags, "
    "watchlist_today, avoid_today, earnings_drift_candidates, recommended_stance, "
    "position_size_override.\n"
    "TICKER SYMBOLS: watchlist_today, avoid_today, and news_flags[].stock MUST use only "
    "ASCII uppercase letters (A–Z), digits, '&', or '-'. NEVER use Unicode, Cyrillic, or "
    "visually-similar non-ASCII characters in any stock symbol. Example: HINDZINC not HINDЗИНК.\n"
    "FIELD SCHEMAS:\n"
    "• news_flags[]: {type (snake_case ≤4 words, e.g. EARNINGS_BEAT|FII_SELLING|"
    "REGULATORY_ACTION|MACRO_DATA|SECTOR_NEWS — never a full sentence), "
    "sentiment (POSITIVE|NEGATIVE|NEUTRAL), urgency (HIGH|MEDIUM|LOW), stock (nullable), "
    "beat_pct (nullable float — non-null ONLY when a confirmed beat % is explicitly cited "
    "in the headline), headline (exact title start, ≤120 chars — do not paraphrase)}\n"
    "• earnings_drift_candidates[]: {stock, beat_pct (null for upcoming results — never fabricate)}\n"
    "• recommended_stance: FULL_SIZE_POSITIONS|HALF_SIZE_POSITIONS|AVOID_TRADING\n"
    "• position_size_override: null unless warranted (e.g. 'REDUCE_50PCT' at VIX STRESS)\n"
    "• sgx_nifty: when source='nse_gift_nifty' this is the REAL GIFT Nifty (NSE IX) "
    "overnight futures — an actual tradeable Nifty opening-gap indicator. When "
    "source='es_futures' it is an estimate from S&P 500 futures (ES=F) × 0.65 "
    "Nifty/SPX beta, not GIFT Nifty.\n\n"

    "NEWS RULES:\n"
    "• age_minutes <180: HIGH relevance (fresh overnight catalyst, not yet priced in). "
    ">720: background context only.\n"
    "• stock_tag present → confirmed company-specific. No tag → infer from title.\n"
    "• Source priority: economic_times, business_standard > google_news.\n"
    "• SEBI/court/promoter pledge/block deal > routine analyst target change in urgency.\n"
    "• Output ≤15 flags. Include only HIGH and MEDIUM urgency — drop LOW entirely.\n\n"

    "OVERNIGHT GAP + ASIAN SESSION:\n"
    "• GAP_UP (>+0.2%): bullish lean → FULL_SIZE_POSITIONS if other signals agree.\n"
    "• GAP_DOWN (<-0.2%): bearish lean; >0.5% gap → HALF_SIZE_POSITIONS even on BULLISH bias.\n"
    "• FLAT: no directional edge — rely on other signals.\n"
    "• nikkei.signal: independent Asian risk (~0.5 Nifty correlation, Tokyo open at 6 AM IST).\n"
    "  NEGATIVE + GAP_DOWN together → strong pan-Asian risk-off, BEARISH, HALF_SIZE_POSITIONS.\n"
    "  POSITIVE + GAP_UP together → broad risk-on, adds BULLISH conviction.\n\n"

    "INDIA VIX (30-day implied vol of Nifty options — overrides directional signals):\n"
    "• LOW (<14): FULL_SIZE_POSITIONS, momentum strategies work.\n"
    "• NORMAL (14–20): default stance.\n"
    "• ELEVATED (20–25): compress bias_confidence ~20%, HALF_SIZE_POSITIONS even if BULLISH.\n"
    "• STRESS (>25): AVOID_TRADING (HALF_SIZE_POSITIONS only if catalyst is extremely clear); "
    "position_size_override='REDUCE_50PCT'. BULLISH bias + STRESS → still AVOID_TRADING or HALF_SIZE.\n"
    "• UNKNOWN: treat as NORMAL.\n\n"

    "COMMODITIES:\n"
    "• crude_oil.change_pct (WTI overnight):\n"
    "  >+2%: BEARISH downstream consumers — BPCL, HPCL, IOC (margin compression), "
    "IndiGo/aviation (fuel costs), Asian Paints/Pidilite (raw material costs) → avoid_today. "
    "BULLISH upstream — ONGC, Oil India → watchlist_today.\n"
    "  <-2%: reverse of above.\n"
    "  -2% to +2%: no sector adjustment.\n"
    "• gold.change_pct (gold futures overnight):\n"
    "  >+1% + FLAT/GAP_DOWN sgx_nifty: compress bias_confidence 10–15% (risk-off caps upside).\n"
    "  >+1% + DXY STRENGTHENING: strong safe-haven demand → BEARISH/NEUTRAL, HALF_SIZE_POSITIONS.\n"
    "  <-0.5%: risk-on, mildly bullish.\n"
    "  >+1%: modestly BULLISH for TITAN (jewellery demand).\n\n"

    "NSE SECTOR INDICES (each entry: current, previous_close, percent_change):\n"
    "• BROAD SELLOFF — 7+ sectors negative, cyclicals (BANK, IT, AUTO, METAL) leading: "
    "high-conviction BEARISH; raise bias_confidence 0.05–0.10.\n"
    "• DEFENSIVE ROTATION — cyclicals down, PHARMA/FMCG flat/positive: NEUTRAL not BEARISH "
    "(money rotating, not fleeing). HALF_SIZE_POSITIONS, favour defensive-sector stocks.\n"
    "• BROAD RALLY — 7+ sectors positive: adds BULLISH conviction.\n"
    "• NIFTY BANK (~35% of Nifty 50): a 2%+ BANK move dominates the index more than any "
    "other sector — weight it accordingly.\n\n"

    "DXY + USD/INR:\n"
    "• dxy.trend: STRENGTHENING = USD rising vs basket; WEAKENING = USD falling.\n"
    "• usdinr.trend: INR_WEAKENING = rupee depreciating — most India-specific bearish signal "
    "(FIIs sell to avoid FX loss on repatriation) → compress bias_confidence 10%.\n"
    "• INR_STRENGTHENING: mildly bullish, supports FII inflows.\n"
    "• DXY STRENGTHENING + INR_WEAKENING: strong EM risk-off → BEARISH or HALF_SIZE_POSITIONS.\n\n"

    "EARNINGS CALENDAR:\n"
    "• Results today/tomorrow: highest uncertainty → avoid_today unless strong positive catalyst.\n"
    "• Results in 3–7 days: moderate — watchlist_today if bias BULLISH and no negative news.\n"
    "• Populate earnings_drift_candidates for every stock in earnings_calendar; beat_pct=null "
    "(upcoming, unconfirmed — never fabricate). Empty calendar → earnings_drift_candidates=[].\n"
    "• Upcoming results + VIX ELEVATED/STRESS: doubly uncertain → lean avoid_today.\n\n"

    "CORPORATE ANNOUNCEMENTS (corporate_announcements — recent NSE filings, watchlist stocks):\n"
    "• Each entry: {symbol, category, summary, time, industry}. These are exchange-filed\n"
    "  overnight catalysts — higher confidence than press/news because they are official.\n"
    "• POSITIVE catalysts (large order wins, capex, fundraising at premium, buyback, "
    "strong pre-results guidance, credit-rating upgrade): → watchlist_today; may raise "
    "bias_confidence ≤0.05 when aligned with overall bias. Emit a news_flag "
    "(type SECTOR_NEWS/EARNINGS_BEAT as appropriate, sentiment POSITIVE).\n"
    "• NEGATIVE catalysts (SEBI/regulatory action, resignation of CEO/CFO/auditor, "
    "rating downgrade, fundraising at steep discount, pledge invocation): → avoid_today. "
    "Emit a news_flag (type REGULATORY_ACTION, sentiment NEGATIVE, urgency HIGH).\n"
    "• Routine/administrative filings (record date, AGM notice, trading-window closure, "
    "investor-meet schedule) carry NO directional signal — ignore for bias.\n"
    "• Only reason about symbols actually present in corporate_announcements.\n\n"

    "RBI / MACRO POLICY (rbi_updates — recent RBI press releases & notifications):\n"
    "• Each entry: {title, category, published, age_hours}. Macro context only — never "
    "set a single-stock bias from these.\n"
    "• Repo-rate CUT or surprise liquidity injection (OMO/VRR): BULLISH for rate-sensitives "
    "— banks, NBFCs (BAJFINANCE), autos, realty → may add BULLISH conviction.\n"
    "• Repo-rate HIKE or liquidity tightening (CRR hike, VRRR): BEARISH for the same set; "
    "compress bias_confidence ~10%.\n"
    "• Regulatory penalty / business restriction on a specific bank/NBFC: stock-specific "
    "NEGATIVE → avoid_today for that name if it is in the watchlist.\n"
    "• Status-quo policy / routine auctions / administrative circulars: no directional signal.\n"
    "• age_hours >48 → background only.\n\n"

    "BULK / BLOCK DEALS (bulk_deals — previous session, watchlist stocks only):\n"
    "• Institutional BUY (mutual fund, FPI, insurance co): accumulation signal → prefer for "
    "watchlist_today; may raise bias_confidence ≤0.05 when aligned with overall bias.\n"
    "• Institutional SELL: distribution signal → demote or move to avoid_today.\n"
    "• Single deal = weak evidence; multiple same-side deals in one stock = strong. "
    "Only reason about symbols actually present in bulk_deals.\n\n"

    "DELIVERY % (delivery_pct — previous session, watchlist stocks only):\n"
    "• >60%: conviction positioning (real delivery, not intraday churn) → prefer in watchlist_today.\n"
    "• <25%: speculative churn → down-weight breakout/momentum conviction.\n"
    "• Quality filter only — never use to set market_bias direction.\n\n"

    "BIAS_CONFIDENCE CALIBRATION (err toward the lower end of each band):\n"
    "• 0.25–0.35: Conflicted or data missing. Default floor. Budget eve, election day, "
    "global shock openings.\n"
    "• 0.40–0.50: 1–2 signals agree, ≥1 material contra-signal. Most Indian sessions.\n"
    "• 0.55–0.65: 3–4 signals agree, no significant contradiction "
    "(e.g. GAP_UP + US positive + FII buying + NORMAL VIX).\n"
    "• 0.65–0.75: 4–5 signals including ≥2 of {GIFT Nifty, FII, VIX, sector breadth} "
    "aligned. ~1–2 sessions/week.\n"
    "• 0.75–0.85: 5+ signals fully aligned, no contradiction. ~1–2 sessions/fortnight.\n"
    "• >0.85: Major surprise only — RBI off-cycle cut/hike, Budget shock, "
    "circuit-breaker morning. Genuinely rare.\n"
    "Hard caps: never >0.70 at VIX ELEVATED; never >0.50 when any HIGH-urgency negative "
    "flag present. FII flows and pre-market RBI/SEBI circulars can reverse any setup."
)


async def _fetch_earnings_calendar(symbols: list[str] | None) -> list[dict]:
    """Upcoming earnings dates, preferring NSE's authoritative event-calendar.

    Non-regression strategy: the NSE event-calendar is the source companies file
    their board-meeting dates with, so it is preferred when it returns data.  If
    NSE is empty (genuinely quiet week, or a silent block) or raises, we fall back
    to the existing Yahoo Finance calendar.  Both return the identical
    {"stock", "earnings_date"} schema, so downstream earnings_drift logic is
    unchanged and the worst case is exactly today's behaviour.
    """
    try:
        nse_events = await fetch_event_calendar(symbols)
        if nse_events:
            return nse_events
        logger.info("NSE event-calendar empty — falling back to Yahoo earnings calendar")
    except Exception as exc:
        logger.warning("NSE event-calendar failed (%s) — falling back to Yahoo", exc)
    return await fetch_earnings_calendar(symbols)


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
        nse_indices, usdinr, nikkei, bulk_deals, delivery_data,
        gift_nifty, corp_announcements, rbi_updates,
    ) = await asyncio.gather(
        fetch_fii_dii_data(),
        fetch_us_market_close(),
        fetch_dxy(),
        fetch_sgx_nifty(),
        fetch_india_vix(),
        fetch_crude_oil(),
        fetch_gold(),
        _fetch_earnings_calendar(prior_watchlist),
        _aggregator.fetch_all(watchlist=prior_watchlist),
        fetch_corporate_actions_today(),
        fetch_nse_indices(),
        fetch_usdinr(),
        fetch_nikkei(),
        fetch_bulk_deals(prior_watchlist),
        fetch_delivery_data(prior_watchlist),
        fetch_gift_nifty(),
        fetch_corporate_announcements(prior_watchlist),
        fetch_rbi_updates(),
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

    # ── GIFT Nifty override (authoritative pre-market gap) ────────────────────
    # GIFT Nifty (NSE IX) trades overnight and is the single best free indicator
    # of the Nifty 50 opening gap at 6 AM IST — strictly better than both the
    # ES=F × 0.65 synthetic proxy (fetch_sgx_nifty) and the stale NSE cash close.
    # When available it takes precedence; the proxy remains the fallback.
    if not isinstance(gift_nifty, Exception) and gift_nifty.get("available"):
        sgx_nifty = {
            "value": gift_nifty["value"],
            "change_pct": gift_nifty["change_pct"],
            "signal": gift_nifty["signal"],
            "source": "nse_gift_nifty",
            "expiry": gift_nifty.get("expiry"),
        }
        logger.info(
            "Nifty gap overridden from GIFT Nifty: %.2f (%.3f%%) signal=%s",
            gift_nifty["value"], gift_nifty["change_pct"], gift_nifty["signal"],
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
        # corporate_announcements: recent NSE filings for watchlist stocks (overnight
        # catalysts — order wins, fundraising, regulatory actions).  Interpretation
        # rules are in RESEARCH_SYSTEM_PROMPT under "CORPORATE ANNOUNCEMENTS".
        "corporate_announcements": corp_announcements if not isinstance(corp_announcements, Exception) else [],
        # rbi_updates: recent RBI press releases / notifications for macro-policy
        # context.  Interpretation rules are in RESEARCH_SYSTEM_PROMPT under
        # "RBI / MACRO POLICY".
        "rbi_updates": rbi_updates if not isinstance(rbi_updates, Exception) else [],
        # bulk_deals: previous-session institutional bulk/block deals filtered to
        # the watchlist.  Interpretation rules are in RESEARCH_SYSTEM_PROMPT under
        # "BULK / BLOCK DEAL INTERPRETATION RULES".
        "bulk_deals": (
            bulk_deals.get("deals", [])
            if not isinstance(bulk_deals, Exception) and bulk_deals.get("available")
            else []
        ),
        # delivery_pct: previous-session delivery percentage per watchlist stock.
        # Interpretation rules are in RESEARCH_SYSTEM_PROMPT under
        # "DELIVERY PERCENTAGE INTERPRETATION RULES".
        "delivery_pct": (
            delivery_data.get("delivery_pct", {})
            if not isinstance(delivery_data, Exception) and delivery_data.get("available")
            else {}
        ),
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
        "JIOFIN":     ["jio financial", "jio finance"],
        "SHRIRAMFIN": ["shriram finance"],
        "HDFCLIFE":   ["hdfc life"],
        "SBILIFE":    ["sbi life"],
        # ── Information Technology ────────────────────────────────────────────
        "INFY":       ["infosys"],
        "TCS":        ["tata consultancy", " tcs "],
        "WIPRO":      ["wipro"],
        "HCLTECH":    ["hcl tech", "hcl technologies"],
        "TECHM":      ["tech mahindra"],
        # ── Consumer / FMCG & Retail ─────────────────────────────────────────
        "RELIANCE":   ["reliance industries", "reliance jio", "reliance retail", "reliance"],
        "HINDUNILVR": ["hindustan unilever", " hul "],
        "NESTLEIND":  ["nestle india"],
        "ITC":        ["itc limited", "itc ltd", " itc "],
        "TATACONSUM": ["tata consumer"],
        "TRENT":      ["trent limited", "zudio", "westside"],
        "ETERNAL":    ["zomato", "eternal limited", "blinkit"],
        # ── Automobiles ──────────────────────────────────────────────────────
        "MARUTI":     ["maruti suzuki", "maruti"],
        # TATAMOTORS demerged: news mentioning "tata motors" is tagged to the passenger-vehicle entity.
        "TMPV":       ["tata motors", "tata passenger vehicles", "jaguar land rover", "jlr"],
        "M&M":        ["mahindra & mahindra", "mahindra and mahindra"],
        "BAJAJ-AUTO": ["bajaj auto"],
        "EICHERMOT":  ["eicher motors", "royal enfield"],
        # ── Aviation ─────────────────────────────────────────────────────────
        "INDIGO":     ["indigo airlines", "interglobe aviation"],
        # ── Metals & Mining ───────────────────────────────────────────────────
        "TATASTEEL":  ["tata steel"],
        "JSWSTEEL":   ["jsw steel"],
        "HINDALCO":   ["hindalco"],
        "COALINDIA":  ["coal india"],
        # ── Energy & Utilities ────────────────────────────────────────────────
        "ONGC":       ["oil and natural gas corporation", "ongc"],
        "NTPC":       ["ntpc"],
        "POWERGRID":  ["power grid corporation", "powergrid"],
        # ── Healthcare & Pharmaceuticals ──────────────────────────────────────
        "SUNPHARMA":  ["sun pharma", "sun pharmaceutical"],
        "DRREDDY":    ["dr. reddy", "dr reddy"],
        "CIPLA":      ["cipla"],
        "MAXHEALTH":  ["max healthcare"],
        # ── Cement & Construction ─────────────────────────────────────────────
        "ULTRACEMCO": ["ultratech cement"],
        "GRASIM":     ["grasim"],
        "LT":         ["larsen & toubro", "larsen and toubro", "l&t"],
        # ── Defence & Industrials ─────────────────────────────────────────────
        "BEL":        ["bharat electronics", " bel "],
        # ── Diversified / Others ─────────────────────────────────────────────
        "TITAN":      ["titan company"],
        "ASIANPAINT": ["asian paints"],
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
        thinking_budget=1500,  # Extended thinking: Claude reasons through signal
                               # contradictions before committing to the JSON output.
                               # Billed as output tokens (~$0.022/call extra at $15/MTok).
                               # Requires temperature=1 (set automatically in the client).
    )
    if brief is None:
        logger.warning("LLM failed — falling back to mock brief")
        prior_wl = await _async_load_prior_watchlist()
        return _generate_mock_brief(raw_data, prior_watchlist=prior_wl)
    return brief


def _parse_generated_at(value: str) -> "time":
    """Parse HH:MM:SS from the LLM-supplied generated_at string.

    The LLM occasionally returns microseconds (``HH:MM:SS.ffffff``) or a
    full ISO timestamp; slice to the first 8 characters so strptime never
    raises on unexpected suffixes.
    """
    from datetime import time as _time
    try:
        return datetime.strptime(value[:8], "%H:%M:%S").time()
    except (ValueError, TypeError):
        logger.warning("Could not parse generated_at=%r — defaulting to now", value)
        return datetime.now(IST).time().replace(microsecond=0)


async def persist_and_publish(brief: MarketBriefLLMOutput) -> None:
    """Save the Market Brief to PostgreSQL and publish it to Redis.

    Uses an upsert pattern: if a brief for today's date already exists
    (e.g. manual trigger after the scheduler), the existing row is
    updated rather than inserting a duplicate.
    """
    brief_dict = brief.model_dump()
    brief_date = datetime.strptime(brief.date, "%Y-%m-%d").date()
    gen_time = _parse_generated_at(brief.generated_at)

    # Persist to PostgreSQL — upsert
    async with get_db_context() as session:
        existing = await session.execute(
            select(MarketBrief).where(MarketBrief.date == brief_date)
        )
        db_brief = existing.scalars().first()
        if db_brief is not None:
            # Update existing row
            db_brief.generated_at = gen_time
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
                generated_at=gen_time,
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


async def run_research_agent(skip_if_trades_exhausted: bool = False) -> None:
    """
    Main entry point for the Research Agent.
    Called by APScheduler at 6:00 AM IST, or triggered manually via the API.

    skip_if_trades_exhausted: if True (midday run), skip entirely when the daily
    trade limit is already reached — no point refreshing the brief if no further
    trades can be placed today.
    """
    if is_nse_holiday():
        logger.info("Research Agent: today is an NSE holiday — skipping run")
        return

    if skip_if_trades_exhausted:
        from core.redis_client import get_value as _get_value  # avoid circular at module level
        from core.redis_keys import DAILY_TRADE_COUNT_KEY
        from core.config import get_settings as _get_settings
        trade_count_str = await _get_value(DAILY_TRADE_COUNT_KEY)
        trade_count = int(trade_count_str) if trade_count_str else 0
        max_trades = _get_settings().max_trades_per_day
        if trade_count >= max_trades:
            logger.info(
                "Research Agent (midday): skipping — daily trade limit already reached "
                "(%d/%d trades used)", trade_count, max_trades
            )
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
