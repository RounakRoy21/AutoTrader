"""
Canonical NIFTY 50 coverage registry + constituent-drift diffing.

The application trades NIFTY 50 stocks and hardcodes their ticker symbols in
several places (news search terms, keyword matchers, earnings defaults, fallback
watchlists).  When NSE reconstitutes the index — scheduled semi-annual reviews,
or off-cycle corporate actions such as the TATAMOTORS → TMPV + TMCV demerger —
those hardcoded maps go stale: a new entrant gets no news coverage, and a removed
name lingers.

This module is the single source of truth for *which symbols the app covers* and
*where that coverage lives*.  A scheduled monitor (agents/nifty50_monitor.py)
fetches the live official constituents and diffs them against TRACKED_SYMBOLS,
alerting the operator with the exact symbols to add/remove and the files to edit.

This module is intentionally pure (no I/O) so it can be imported anywhere and
unit-tested trivially.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

# ── Coverage registry ──────────────────────────────────────────────────────────
# Every NIFTY 50 symbol the app currently has dedicated handling for.  This MUST
# stay in sync with the hardcoded maps listed in COVERAGE_LOCATIONS below.  The
# set is deliberately a touch broader than exactly-50 because it also covers
# demerger entities (TMPV + TMCV) whose parent (TATAMOTORS) was split.
TRACKED_SYMBOLS: frozenset[str] = frozenset({
    # Financials
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "SBIN", "AXISBANK",
    "BAJFINANCE", "BAJAJFINSV", "JIOFIN", "SHRIRAMFIN", "HDFCLIFE", "SBILIFE",
    # Information Technology
    "INFY", "TCS", "WIPRO", "HCLTECH", "TECHM",
    # Consumer / FMCG & Retail
    "RELIANCE", "HINDUNILVR", "NESTLEIND", "ITC", "TATACONSUM", "TRENT", "ETERNAL",
    # Automobiles
    "MARUTI", "TMPV", "M&M", "BAJAJ-AUTO", "EICHERMOT",
    # Aviation
    "INDIGO",
    # Metals & Mining
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "COALINDIA",
    # Energy & Utilities
    "ONGC", "NTPC", "POWERGRID",
    # Healthcare & Pharmaceuticals
    "SUNPHARMA", "DRREDDY", "CIPLA", "MAXHEALTH",
    # Cement & Construction
    "ULTRACEMCO", "GRASIM", "LT",
    # Defence & Industrials
    "BEL",
    # Diversified / Others
    "TITAN", "ASIANPAINT", "APOLLOHOSP", "BHARTIARTL",
    "ADANIPORTS", "ADANIENT",
})

# Symbols NSE may list as NIFTY 50 members that map onto an entity the app already
# covers under a different ticker (corporate actions / demergers / renames).  Keys
# are the *NSE-listed* symbol; values are the app symbol(s) that cover it.  This
# prevents the monitor from screaming "uncovered new entrant!" when the underlying
# business is already handled.  Extend this as corporate actions occur.
SYMBOL_ALIASES: Dict[str, List[str]] = {
    # The TATAMOTORS demerger: NSE may still list TATAMOTORS (or TATAMTRDVR) while
    # the app tracks the demerged passenger-vehicle entity TMPV.
    "TATAMOTORS": ["TMPV"],
    "TATAMTRDVR": ["TMPV"],
    # Zomato was renamed to Eternal Limited in 2025; some feeds still use the old ticker.
    "ZOMATO": ["ETERNAL"],
}

# Where NIFTY 50 ticker symbols are hardcoded.  When the monitor reports drift,
# these are the files to update together so coverage stays consistent.
COVERAGE_LOCATIONS: List[str] = [
    "integrations/news_aggregator.py :: HybridNewsAggregator._STOCK_SEARCH_TERMS  (symbol → Google News query)",
    "agents/research_agent.py        :: _parse_news_flags.STOCK_KEYWORDS          (symbol → headline match phrases)",
    "agents/research_agent.py        :: _FALLBACK_WATCHLIST                        (cold-start watchlist subset)",
    "integrations/alpha_vantage_client.py :: _EARNINGS_DEFAULT_SYMBOLS            (earnings calendar default subset)",
    "integrations/nse_client.py      :: _EVENT_CALENDAR_DEFAULT_SYMBOLS           (event calendar default subset)",
    "core/nifty50.py                 :: TRACKED_SYMBOLS                            (this registry — update first)",
]


def _normalise(symbols: Iterable[str]) -> set[str]:
    return {s.strip().upper() for s in symbols if s and s.strip()}


def _is_covered(symbol: str, tracked: set[str]) -> bool:
    """A live symbol is covered if it is tracked directly or via an alias mapping."""
    if symbol in tracked:
        return True
    aliases = SYMBOL_ALIASES.get(symbol)
    return bool(aliases) and any(a in tracked for a in aliases)


def diff_constituents(
    live_symbols: Iterable[str],
    previous_symbols: Iterable[str] | None = None,
) -> Dict[str, Any]:
    """Compare the live official NIFTY 50 against the app's coverage and history.

    Args:
        live_symbols: symbols from the latest NSE constituents fetch.
        previous_symbols: the constituents seen at the previous check (from Redis),
            used to report period-over-period index membership changes.

    Returns a structured report:
        live_count                  — number of live constituents
        new_entrants_uncovered      — live symbols with NO app coverage → ACTION REQUIRED
        tracked_not_in_index        — tracked symbols no longer in the index → review/cleanup
        index_additions_since_last  — entered the index since the previous check
        index_removals_since_last   — left the index since the previous check
        has_coverage_gap            — bool: any uncovered new entrant
        has_membership_change       — bool: any add/remove vs previous check
    """
    live = _normalise(live_symbols)
    tracked = set(TRACKED_SYMBOLS)

    new_entrants_uncovered = sorted(s for s in live if not _is_covered(s, tracked))
    tracked_not_in_index = sorted(
        s for s in tracked
        if s not in live and not _aliased_into_live(s, live)
    )

    if previous_symbols is not None:
        prev = _normalise(previous_symbols)
        index_additions = sorted(live - prev)
        index_removals = sorted(prev - live)
    else:
        index_additions = []
        index_removals = []

    return {
        "live_count": len(live),
        "new_entrants_uncovered": new_entrants_uncovered,
        "tracked_not_in_index": tracked_not_in_index,
        "index_additions_since_last": index_additions,
        "index_removals_since_last": index_removals,
        "has_coverage_gap": bool(new_entrants_uncovered),
        "has_membership_change": bool(index_additions or index_removals),
    }


def _aliased_into_live(tracked_symbol: str, live: set[str]) -> bool:
    """True if a tracked symbol corresponds to a live NSE symbol via an alias.

    Prevents the demerged TMPV/TMCV (which the app tracks) from being reported as
    "no longer in the index" when NSE still lists the parent TATAMOTORS.
    """
    for nse_symbol, app_symbols in SYMBOL_ALIASES.items():
        if tracked_symbol in app_symbols and nse_symbol in live:
            return True
    return False
