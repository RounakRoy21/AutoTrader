"""
Backtest trade simulator (audit V2 / V4 / V5 / V6).

Pure functions over candles — no network, no DB — so they are unit-testable:

  * ``simulate_trade``: one signal → one simulated MIS trade using the LIVE
    exit policy (``agents.exit_policy``: stop, scale-out + breakeven, ROI decay,
    target, trailing, 15:00 square-off), production sizing
    (``signal_rules.size_position``), entry at the NEXT bar's open, adverse
    slippage on every fill (more on stops), stops filled at min(stop, price)
    and round-trip costs per leg (``_transaction_costs``).
  * ``apply_portfolio_rules``: the live portfolio gates on one clock — max
    open positions, max trades per day / per symbol, one position per symbol,
    stock lock after a net loss and the daily drawdown halt.
  * ``compute_metrics``: ₹ equity curve, net expectancy (₹ and R), profit
    factor over ALL exits, max drawdown, worst day, halt days, Sharpe /
    Sortino and a bootstrap CI for the expectancy.
  * ``walk_forward_folds`` / ``split_holdout``: out-of-sample reporting.

Intra-candle price path: O→L→H→C for a bullish candle, O→H→L→C for a bearish
one (the same assumption as the tick replay).  A level touched inside the
candle fills AT the level; a candle that opens through a level fills at the
open (gap).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from agents import exit_policy, signal_rules
from agents.position_service import stop_exit_reason
from agents.risk_manager import _transaction_costs
from core.tick_size import get_tick_size

SENSITIVITY_BPS = (0.0, 5.0, 10.0, 20.0)


@dataclass(frozen=True)
class SlippageModel:
    """Adverse slippage in basis points per fill; stop / gap fills slip
    ``stop_mult`` times more."""

    bps: float = 5.0
    stop_mult: float = 2.0

    def buy(self, price: float) -> float:
        return price * (1 + self.bps / 10_000)

    def sell(self, price: float, stop: bool = False) -> float:
        return price * (1 - self.bps * (self.stop_mult if stop else 1.0) / 10_000)


@dataclass
class SignalCandidate:
    symbol: str
    signal_ts: datetime
    ltp: float
    vwap: float
    rsi: float
    vol_ratio: float
    rsi_5m: Optional[float]
    ema_9: float
    ema_21: float
    macd_hist: float
    atr: float
    n_candles: int
    future: pd.DataFrame = field(repr=False)    # same-day candles after the signal candle


@dataclass
class SimTrade:
    symbol: str
    trade_date: date
    signal_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    signal_ltp: float
    entry_price: float
    qty: int
    initial_sl: float
    initial_target: float
    exit_price: float               # quantity-weighted over all exit legs
    exit_reason: str
    legs: List[Tuple[int, float]]
    gross_pnl: float
    costs: float
    net_pnl: float
    risk_rupees: float
    r_multiple: float
    rsi: float = 0.0
    vol_ratio: float = 0.0
    atr: float = 0.0

    @property
    def outcome(self) -> str:
        return "WIN" if self.net_pnl > 0 else ("LOSS" if self.net_pnl < 0 else "FLAT")


def _path(row: Any) -> List[float]:
    o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
    return [o, l, h, c] if c >= o else [o, h, l, c]


def simulate_trade(
    cand: SignalCandidate,
    settings: Any,
    slip: SlippageModel,
) -> Optional[SimTrade]:
    """Simulate the trade a signal would open; None when it cannot be entered
    (no next bar, or the next bar is at / after the 15:00 square-off)."""
    future = cand.future
    if future is None or future.empty:
        return None
    rows = list(future.itertuples(index=False))
    entry_row = rows[0]
    entry_ts: datetime = entry_row.timestamp
    if entry_ts.time() >= exit_policy.MIS_CLOSE_START:
        return None

    tick = get_tick_size(cand.symbol)
    entry = slip.buy(float(entry_row.open))
    sl, tgt = exit_policy.initial_levels(entry, cand.atr, settings, tick)
    qty = signal_rules.size_position(entry, cand.atr, cand.n_candles, settings)
    if qty < 1 or sl >= entry:
        return None
    pos = SimpleNamespace(
        entry_price=entry, stop_loss_price=sl, target_price=tgt, quantity=qty,
        partial_booked=False, partial_target_price=exit_policy.partial_target(entry, sl, tgt, qty, settings),
        stop_kind=None, product_type="MIS", trade_date=entry_ts.date(), entry_time=entry_ts.time(),
    )

    legs: List[Tuple[int, float]] = []
    reason = "END_OF_DATA"
    exit_ts = entry_ts
    done = False
    for row in rows:
        now: datetime = row.timestamp
        exit_ts = now
        if exit_policy.square_off_due(pos, now.time()):
            legs.append((pos.quantity, slip.sell(float(row.open))))
            reason = "EOD_CLOSE"
            done = True
            break
        for i, px in enumerate(_path(row)):
            at_open = i == 0
            if exit_policy.stop_hit(pos, px):
                fill = px if at_open else pos.stop_loss_price
                legs.append((pos.quantity, slip.sell(min(fill, pos.stop_loss_price), stop=True)))
                reason = stop_exit_reason(pos)
                done = True
                break
            if exit_policy.partial_due(pos, px, settings):
                book, remaining = exit_policy.partial_split(pos.quantity, settings)
                if remaining >= 1:
                    fill = px if at_open else pos.partial_target_price
                    legs.append((book, slip.sell(fill)))
                    pos.quantity = remaining
                    pos.partial_booked = True
                    new_sl = exit_policy.breakeven_stop(pos, settings, tick)
                    if new_sl > pos.stop_loss_price:
                        pos.stop_loss_price, pos.stop_kind = new_sl, "BREAKEVEN"
            if settings.roi_decay_enabled:
                decayed = exit_policy.roi_decay_target(pos, now, cand.atr, tick)
                if decayed is not None:
                    pos.target_price = decayed
            if exit_policy.target_hit(pos, px):
                fill = px if at_open else pos.target_price
                legs.append((pos.quantity, slip.sell(fill)))
                reason = "TARGET_HIT"
                done = True
                break
            trailed = exit_policy.trailing_stop(pos, px, settings, tick)
            if trailed is not None:
                pos.stop_loss_price, pos.stop_kind = trailed, "TRAILING"
        if done:
            break
    if not done:
        legs.append((pos.quantity, slip.sell(float(rows[-1].close))))

    sold = sum(q for q, _ in legs)
    gross = sum((px - entry) * q for q, px in legs)
    costs = sum(_transaction_costs(entry, px, q, "MIS") for q, px in legs)
    net = gross - costs
    risk = (entry - sl) * qty
    return SimTrade(
        symbol=cand.symbol,
        trade_date=entry_ts.date(),
        signal_ts=cand.signal_ts,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        signal_ltp=cand.ltp,
        entry_price=round(entry, 2),
        qty=qty,
        initial_sl=sl,
        initial_target=tgt,
        exit_price=round(sum(px * q for q, px in legs) / sold, 2) if sold else entry,
        exit_reason=reason,
        legs=legs,
        gross_pnl=round(gross, 2),
        costs=round(costs, 2),
        net_pnl=round(net, 2),
        risk_rupees=round(risk, 2),
        r_multiple=round(net / risk, 3) if risk > 0 else 0.0,
        rsi=cand.rsi,
        vol_ratio=cand.vol_ratio,
        atr=cand.atr,
    )


# ── Portfolio rules on one clock ─────────────────────────────────────────────

def apply_portfolio_rules(
    trades: Sequence[SimTrade], settings: Any,
) -> Tuple[List[SimTrade], Dict[str, int], set]:
    """Accept trades in entry order under the live portfolio gates.

    Returns ``(accepted, rejected_by_rule_counts, halted_days)``.
    """
    accepted: List[SimTrade] = []
    rejected: Dict[str, int] = defaultdict(int)
    halted_days: set = set()
    limit = settings.daily_drawdown_limit
    for t in sorted(trades, key=lambda x: (x.entry_ts, x.symbol)):
        day = [a for a in accepted if a.trade_date == t.trade_date]
        closed = [a for a in day if a.exit_ts <= t.entry_ts]
        realised_loss = sum(-a.net_pnl for a in closed if a.net_pnl < 0)
        if realised_loss >= limit:
            halted_days.add(t.trade_date)
            rejected["DAILY_HALT"] += 1
            continue
        if getattr(settings, "stock_lock_after_sl", False) and any(
            a.symbol == t.symbol and a.net_pnl < 0 for a in closed
        ):
            rejected["STOCK_LOCK"] += 1
            continue
        open_now = [a for a in day if a.exit_ts > t.entry_ts]
        if any(a.symbol == t.symbol for a in open_now):
            rejected["ALREADY_OPEN"] += 1
            continue
        if len(open_now) >= settings.max_open_positions:
            rejected["MAX_OPEN"] += 1
            continue
        if len(day) >= settings.max_trades_per_day:
            rejected["MAX_TRADES_DAY"] += 1
            continue
        if sum(1 for a in day if a.symbol == t.symbol) >= settings.max_trades_per_symbol_per_day:
            rejected["MAX_TRADES_SYMBOL"] += 1
            continue
        accepted.append(t)
    return accepted, dict(rejected), halted_days


# ── Metrics ──────────────────────────────────────────────────────────────────

def _bootstrap_ci(values: np.ndarray, n: int = 2000, seed: int = 0) -> Tuple[float, float]:
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = rng.choice(values, size=(n, len(values)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def compute_metrics(trades: Sequence[SimTrade], settings: Any, halted_days: set = frozenset()) -> Dict[str, Any]:
    """Capital-decision metrics over ALL exits (no TIMEOUT exclusion)."""
    capital = float(settings.total_capital)
    out: Dict[str, Any] = {"trades": len(trades)}
    if not trades:
        return out
    net = np.array([t.net_pnl for t in trades], dtype=float)
    r = np.array([t.r_multiple for t in trades], dtype=float)
    wins, losses = net[net > 0], net[net < 0]

    ordered = sorted(trades, key=lambda t: t.exit_ts)
    equity = capital + np.cumsum([t.net_pnl for t in ordered])
    peaks = np.maximum.accumulate(np.concatenate([[capital], equity]))[1:]
    drawdowns = peaks - equity

    daily: Dict[date, float] = defaultdict(float)
    daily_loss: Dict[date, float] = defaultdict(float)
    for t in trades:
        daily[t.trade_date] += t.net_pnl
        if t.net_pnl < 0:
            daily_loss[t.trade_date] += -t.net_pnl
    day_pnl = np.array(list(daily.values()), dtype=float)
    day_ret = day_pnl / capital
    downside = day_ret[day_ret < 0]
    std = day_ret.std(ddof=1) if len(day_ret) > 1 else 0.0
    dstd = math.sqrt((downside ** 2).mean()) if len(downside) else 0.0

    streak = worst_streak = 0
    for t in ordered:
        streak = streak + 1 if t.net_pnl < 0 else 0
        worst_streak = max(worst_streak, streak)

    halt_limit = settings.daily_drawdown_limit
    lo, hi = _bootstrap_ci(net)
    out.update(
        win_rate_pct=round(len(wins) / len(net) * 100, 1),
        net_pnl=round(float(net.sum()), 2),
        gross_costs=round(sum(t.costs for t in trades), 2),
        expectancy_rupees=round(float(net.mean()), 2),
        expectancy_ci95=(round(lo, 2), round(hi, 2)),
        expectancy_r=round(float(r.mean()), 3),
        profit_factor=round(float(wins.sum() / -losses.sum()), 2) if len(losses) else float("inf"),
        max_drawdown=round(float(drawdowns.max()), 2),
        max_drawdown_pct=round(float(drawdowns.max()) / capital * 100, 2),
        days=len(day_pnl),
        worst_day=round(float(day_pnl.min()), 2),
        best_day=round(float(day_pnl.max()), 2),
        halt_days=len(set(halted_days) | {d for d, loss in daily_loss.items() if loss >= halt_limit}),
        sharpe_daily_ann=round(float(day_ret.mean() / std * math.sqrt(252)), 2) if std > 0 else 0.0,
        sortino_daily_ann=round(float(day_ret.mean() / dstd * math.sqrt(252)), 2) if dstd > 0 else 0.0,
        max_losing_streak=worst_streak,
        exit_reasons=dict(pd.Series([t.exit_reason for t in trades]).value_counts()),
    )
    return out


# ── Out-of-sample splits ─────────────────────────────────────────────────────

def walk_forward_folds(dates: Sequence[date], k: int) -> List[Tuple[date, date]]:
    """``k`` contiguous, non-overlapping (start, end) date folds in order."""
    days = sorted(set(dates))
    if k < 1 or not days:
        return []
    k = min(k, len(days))
    size = math.ceil(len(days) / k)
    return [(chunk[0], chunk[-1]) for chunk in (days[i:i + size] for i in range(0, len(days), size))]


def split_holdout(trades: Sequence[SimTrade], holdout_from: date) -> Tuple[List[SimTrade], List[SimTrade]]:
    """(in-sample, holdout) by trade date.  Never tune parameters on the holdout."""
    ins = [t for t in trades if t.trade_date < holdout_from]
    return ins, [t for t in trades if t.trade_date >= holdout_from]


def trades_frame(trades: Sequence[SimTrade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        row = {k: v for k, v in t.__dict__.items() if k != "legs"}
        row["legs"] = ";".join(f"{q}@{px:.2f}" for q, px in t.legs)
        row["outcome"] = t.outcome
        rows.append(row)
    return pd.DataFrame(rows)
