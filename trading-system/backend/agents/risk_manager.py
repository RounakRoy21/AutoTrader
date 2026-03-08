"""
Risk Manager — Deterministic, rule-based position monitoring.

Runs on a separate thread, polling every 5 seconds. ZERO LLM involvement.
Checks all open positions against stop-loss / target prices, enforces
daily loss limits, and handles intraday square-off at 3:00 PM IST.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from typing import Dict, List, Optional

import pytz
from sqlalchemy import select, update

from core.config import get_settings
from core.database import get_db_context
from core.redis_client import get_value, publish, set_value
from core.redis_keys import HALT_KEY
from integrations.kite_client import get_kite_client
from integrations import ltp_store
from integrations.telegram_client import (
    send_eod_report,
    send_halt_alert,
    send_intraday_close_alert,
    send_stop_loss_alert,
    send_target_hit_alert,
)
from models.daily_pnl import DailyPnl
from models.trade import Trade

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
POLL_INTERVAL = 5  # seconds

# Intraday close times
MIS_CLOSE_START = dt_time(15, 0)   # 3:00 PM IST
MIS_FORCE_CLOSE = dt_time(15, 20)  # 3:20 PM IST
EOD_REPORT_TIME = dt_time(15, 30)  # 3:30 PM IST


class RiskManager:
    """
    Deterministic risk manager that monitors all open positions.
    Rules enforced:
      - Stop-loss exit: ATR-based (entry − ATR × multiplier) or fixed % fallback
      - Target exit: ATR-based (entry + ATR × multiplier) or fixed % fallback
      - Trailing stop-loss activates once price moves above activation threshold
      - ROI decay: target reduced over time (15m→0.8%, 25m→0.3%, 35m→0.1%)
      - Per-stock lock after SL hit (expires after ~8h, before next market open)
      - Consecutive loss pause after N SL hits
      - Max 3 open positions (enforced by Decision Engine)
      - Max 6 trades per day (enforced by Decision Engine)
      - Daily drawdown halt at 3% of capital
      - MIS square-off starting at 3:00 PM, forced by 3:20 PM
      - EOD report at 3:30 PM
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._eod_reported_today = False
        self._intraday_close_initiated = False

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the risk manager on a separate daemon thread."""
        self._loop = loop
        self._running = True
        self._eod_reported_today = False
        self._intraday_close_initiated = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="RiskManager")
        self._thread.start()
        # Publish status so the dashboard can show it
        asyncio.run_coroutine_threadsafe(
            set_value("agent:risk:status", "ACTIVE"), loop
        )
        logger.info("Risk Manager started on separate thread")

    def stop(self) -> None:
        """Stop the risk manager thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                set_value("agent:risk:status", "INACTIVE"), self._loop
            )
        logger.info("Risk Manager stopped")

    def _run_loop(self) -> None:
        """Main polling loop — runs every POLL_INTERVAL seconds."""
        while self._running:
            try:
                future = asyncio.run_coroutine_threadsafe(self._poll(), self._loop)
                future.result(timeout=30)
            except Exception as exc:
                logger.exception("Risk Manager poll error: %s", exc)
            time.sleep(POLL_INTERVAL)

    async def _poll(self) -> None:
        """Single poll cycle: check positions, enforce rules."""
        now_ist = datetime.now(IST)
        current_time = now_ist.time()

        # Skip outside market hours
        if current_time < dt_time(9, 15) or current_time > dt_time(15, 35):
            return

        # Fetch open positions from DB
        open_trades = await self._get_open_trades()
        if not open_trades:
            # Check if it's EOD report time
            if current_time >= EOD_REPORT_TIME and not self._eod_reported_today:
                await self._generate_eod_report()
            return

        # Build a symbol → LTP mapping:
        #   • Paper mode  → LTP store (written by Scanner on every tick)
        #   • Live mode   → Kite REST LTP (real market data)
        settings = self._settings
        ltp_map: Dict[str, float] = {}

        if settings.paper_trading:
            for trade in open_trades:
                price = ltp_store.get_ltp(trade.stock)
                if price is None:
                    # Tick not received yet — fall back to entry price (safe default)
                    price = trade.entry_price
                    logger.debug(
                        "[RiskManager] No tick yet for %s — using entry price",
                        trade.stock,
                    )
                ltp_map[trade.stock] = price
        else:
            kite = get_kite_client()
            try:
                instruments = [f"NSE:{t.stock}" for t in open_trades]
                raw = await kite.get_ltp(instruments)
                ltp_map = {t.stock: raw[f"NSE:{t.stock}"]["last_price"] for t in open_trades
                           if f"NSE:{t.stock}" in raw}
            except Exception as exc:
                logger.error("Failed to fetch LTP for risk check: %s", exc)
                return

        for trade in open_trades:
            if trade.stock not in ltp_map:
                continue
            ltp = ltp_map[trade.stock]

            # ── Stop Loss Check ────────────────────
            if ltp <= trade.stop_loss_price:
                await self._close_position(trade, ltp, "STOP_LOSS_HIT")
                loss = (trade.entry_price - ltp) * trade.quantity
                await send_stop_loss_alert(trade.stock, ltp, loss)
                await publish("system_alerts", {
                    "type": "danger",
                    "message": f"Stop-loss hit: {trade.stock} @ ₹{ltp:.2f} | Loss: ₹{loss:.2f}",
                    "timestamp": now_ist.isoformat(),
                })
                continue

            # ── ROI Decay (time-based target reduction) ──
            # Evaluated BEFORE target check so the reduced target takes effect
            # in the same poll cycle rather than requiring a 5-second delay.
            #
            # Thresholds are calibrated for NSE intraday stocks with ATR-based
            # stops (typically 0.7–2 % below entry).  Original values of 0.8 / 0.3 /
            # 0.1 % created an inverted R:R after 35 min — the trade would close at
            # near-breakeven while still carrying a ₹7–15 downside risk from the SL.
            # The revised curve keeps a meaningful R:R at every tier:
            #
            #   > 20 min → 1.5 %  (still ~1 : 1 R:R for a 1.5 % ATR stop)
            #   > 35 min → 1.0 %  (captures most of the expected intraday move)
            #   > 50 min → 0.5 %  (late-move urgency; MIS force-close is at 15:00)
            if settings.roi_decay_enabled and trade.entry_time:
                entry_dt = IST.localize(datetime.combine(trade.trade_date, trade.entry_time))
                elapsed_min = (now_ist - entry_dt).total_seconds() / 60
                if elapsed_min > 50:
                    decayed = round(trade.entry_price * 1.005, 2)   # 0.5%
                elif elapsed_min > 35:
                    decayed = round(trade.entry_price * 1.010, 2)   # 1.0%
                elif elapsed_min > 20:
                    decayed = round(trade.entry_price * 1.015, 2)   # 1.5%
                else:
                    decayed = None
                # Guard: never let the decayed target fall below or equal to
                # the current stop-loss (which may have trailed upward).
                # Without this, trailing SL + ROI decay can invert the
                # SL/target corridor, trapping the position with no exit path.
                if (
                    decayed is not None
                    and decayed < trade.target_price
                    and decayed > trade.stop_loss_price
                ):
                    old_tgt = trade.target_price
                    await self._update_target(trade, decayed)
                    logger.info(
                        "ROI decay: %s target ₹%.2f → ₹%.2f (%.0f min elapsed)",
                        trade.stock, old_tgt, decayed, elapsed_min,
                    )

            # ── Target Check ───────────────────────
            # Runs AFTER ROI decay so the possibly-reduced target is used.
            if ltp >= trade.target_price:
                await self._close_position(trade, ltp, "TARGET_HIT")
                profit = (ltp - trade.entry_price) * trade.quantity
                await send_target_hit_alert(trade.stock, ltp, profit)
                await publish("system_alerts", {
                    "type": "success",
                    "message": f"Target hit: {trade.stock} @ ₹{ltp:.2f} | Profit: ₹{profit:.2f}",
                    "timestamp": now_ist.isoformat(),
                })
                continue

            # ── Trailing Stop Loss ─────────────────
            # Once price moves above an activation threshold, trail the SL
            # upward with LTP to lock in profits.  The SL is only ever moved
            # *up*, never back down.
            activation = trade.entry_price * (1 + settings.trailing_sl_activation_pct)
            if ltp >= activation:
                trailing_sl = round(ltp * (1 - settings.trailing_sl_trail_pct), 2)
                if trailing_sl > trade.stop_loss_price:
                    old_sl = trade.stop_loss_price
                    await self._update_stop_loss(trade, trailing_sl)
                    logger.info(
                        "Trailing SL: %s ₹%.2f → ₹%.2f (LTP=₹%.2f)",
                        trade.stock, old_sl, trailing_sl, ltp,
                    )

            # ── MIS Intraday Close (3:00 PM) ──────
            if trade.product_type == "MIS" and current_time >= MIS_CLOSE_START:
                if not self._intraday_close_initiated:
                    await send_intraday_close_alert()
                    await publish("system_alerts", {
                        "type": "warning",
                        "message": "MIS square-off initiated — all intraday positions closing",
                        "timestamp": now_ist.isoformat(),
                    })
                    self._intraday_close_initiated = True
                await self._close_position(trade, ltp, "EOD_CLOSE")
                continue

        # ── Daily Drawdown Halt ───────────────────
        await self._check_daily_drawdown(ltp_map)

        # ── EOD Report ────────────────────────────
        if current_time >= EOD_REPORT_TIME and not self._eod_reported_today:
            await self._generate_eod_report()

    async def _get_open_trades(self) -> List[Trade]:
        """Fetch all open trades from the database."""
        async with get_db_context() as session:
            result = await session.execute(
                select(Trade).where(Trade.status == "OPEN")
            )
            return list(result.scalars().all())

    async def _close_position(self, trade: Trade, exit_price: float, reason: str) -> None:
        """Close a position: mark CLOSING in DB, place sell order, then mark CLOSED.

        The two-phase DB update prevents double-sell: if the process crashes after
        SELL but before the final CLOSED update, the position stays ``CLOSING`` and
        will be retried — but the Risk Manager only picks up ``OPEN`` positions, so
        a reconciliation job should handle orphaned ``CLOSING`` rows at EOD.
        """
        now_ist = datetime.now(IST)
        settings = self._settings

        # Phase 1 — Atomically mark CLOSING so no other poll cycle picks this up
        async with get_db_context() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "OPEN")
                .values(status="CLOSING")
            )

        # Phase 1b — Cancel GTT stop-loss order (live mode only)
        # Must cancel BEFORE placing the sell to avoid the GTT firing in
        # the window between sell and cancellation, which would double-sell.
        if not settings.paper_trading and getattr(trade, "gtt_trigger_id", None):
            try:
                kite = get_kite_client()
                await kite.delete_gtt(trade.gtt_trigger_id)
                logger.info("Cancelled GTT trigger_id=%s for %s", trade.gtt_trigger_id, trade.stock)
            except Exception as gtt_exc:
                # Non-fatal: GTT may have already been triggered or expired
                logger.warning(
                    "Could not cancel GTT trigger_id=%s for %s: %s",
                    trade.gtt_trigger_id, trade.stock, gtt_exc,
                )

        # Phase 2 — Place sell order via Kite
        try:
            if not settings.paper_trading:
                kite = get_kite_client()
                await kite.place_order(
                    tradingsymbol=trade.stock,
                    transaction_type="SELL",
                    quantity=trade.quantity,
                    product=trade.product_type,
                    order_type="MARKET",
                )
        except Exception as exc:
            logger.error("Failed to place SELL for %s: %s — keeping CLOSING (manual review needed)", trade.stock, exc)
            # Do NOT revert to OPEN — the sell may have been sent but the
            # response timed out.  Reverting would cause a double-sell on the
            # next poll cycle.  Leave as CLOSING; EOD reconciliation or
            # manual intervention will handle it.
            return

        # Phase 3 — Calculate P&L and finalise as CLOSED
        pnl = (exit_price - trade.entry_price) * trade.quantity

        async with get_db_context() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade.id)
                .values(
                    exit_price=exit_price,
                    exit_reason=reason,
                    exit_time=now_ist.time(),
                    realized_pnl=pnl,
                    status="CLOSED",
                )
            )

        logger.info(
            "%s: %s @ ₹%.2f → P&L: ₹%.2f",
            reason, trade.stock, exit_price, pnl,
        )

        # Publish event to Redis
        await publish("trade_events", {
            "type": "TRADE_CLOSED",
            "stock": trade.stock,
            "exit_price": exit_price,
            "exit_reason": reason,
            "pnl": pnl,
            "timestamp": now_ist.isoformat(),
        })

        # ── Per-stock lock after SL hit ────────────
        # TTL of 28800s (8h) ensures the lock always expires before the next
        # market open (earliest SL=9:15am + 8h = 5:15pm; next open=9:15am).
        if reason == "STOP_LOSS_HIT" and settings.stock_lock_after_sl:
            await set_value(f"stock_lock:{trade.stock}", "TRUE", ttl=28800)
            logger.info("Stock locked after SL: %s (expires in 8h)", trade.stock)

        # ── Consecutive loss tracking ──────────────
        # Only SL hits advance the streak; target hits reset it.
        # EOD_CLOSE and other exits are neutral — they neither advance nor
        # reset the streak, so a morning SL run isn't artificially extended
        # by forced EOD exits.
        if reason == "STOP_LOSS_HIT":
            count_str = await get_value("consecutive_losses") or "0"
            count = int(count_str) + 1
            await set_value("consecutive_losses", str(count))
            if count >= settings.consecutive_loss_pause_threshold:
                pause_until = now_ist + timedelta(minutes=settings.consecutive_loss_pause_minutes)
                await set_value("consecutive_loss_pause_until", pause_until.isoformat())
                logger.warning(
                    "%d consecutive losses — trading paused until %s",
                    count, pause_until.strftime("%H:%M"),
                )
        elif reason == "TARGET_HIT":
            await set_value("consecutive_losses", "0")

    async def _update_stop_loss(self, trade: Trade, new_sl: float) -> None:
        """Update the stop-loss price for a trade in the database (trailing SL)."""
        async with get_db_context() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade.id)
                .values(stop_loss_price=new_sl)
            )
        # Update in-memory so this poll cycle uses the new value
        trade.stop_loss_price = new_sl

        await publish("trade_events", {
            "type": "TRAILING_SL_UPDATE",
            "stock": trade.stock,
            "new_stop_loss": new_sl,
            "timestamp": datetime.now(IST).isoformat(),
        })

    async def _update_target(self, trade: Trade, new_target: float) -> None:
        """Update the target price for a trade in the database (ROI decay)."""
        async with get_db_context() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade.id)
                .values(target_price=new_target)
            )
        trade.target_price = new_target

        await publish("trade_events", {
            "type": "ROI_DECAY_UPDATE",
            "stock": trade.stock,
            "new_target": new_target,
            "timestamp": datetime.now(IST).isoformat(),
        })

    async def _check_daily_drawdown(self, ltp_map: Optional[Dict[str, float]] = None) -> None:
        """If total daily loss (realised + unrealised) exceeds 3% of capital, halt trading.

        Accepts an optional *ltp_map* (symbol → price) from the main poll cycle
        to avoid redundant Kite API calls.  Falls back to ltp_store / Kite when
        the map is not provided or the symbol is missing.
        """
        halt = await get_value(HALT_KEY)
        if halt == "TRUE":
            return  # already halted

        today = date.today()

        # ── Realised losses from closed trades ──
        async with get_db_context() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.trade_date == today,
                    Trade.status == "CLOSED",
                    Trade.realized_pnl < 0,
                )
            )
            closed_losses = result.scalars().all()
        total_realised_loss = sum(abs(t.realized_pnl or 0) for t in closed_losses)

        # ── Unrealised losses from open positions ──
        open_trades = await self._get_open_trades()
        unrealised_loss = 0.0
        settings = self._settings
        for trade in open_trades:
            # Prefer the ltp_map already fetched in the main poll cycle
            ltp = (ltp_map or {}).get(trade.stock)
            if ltp is None:
                if settings.paper_trading:
                    ltp = ltp_store.get_ltp(trade.stock)
                else:
                    try:
                        kite = get_kite_client()
                        raw = await kite.get_ltp([f"NSE:{trade.stock}"])
                        ltp = raw.get(f"NSE:{trade.stock}", {}).get("last_price")
                    except Exception:
                        ltp = None
            if ltp is None:
                ltp = trade.entry_price
            mtm = (ltp - trade.entry_price) * trade.quantity
            if mtm < 0:
                unrealised_loss += abs(mtm)

        total_loss = total_realised_loss + unrealised_loss
        limit = self._settings.daily_drawdown_limit

        # Always track current drawdown in Redis so the dashboard can read it
        drawdown_pct = round((total_loss / limit) * 100, 2) if limit > 0 else 0.0
        await set_value("agent:risk:daily_loss", str(round(total_loss, 2)))
        await set_value("agent:risk:drawdown_pct", str(drawdown_pct))

        if total_loss >= limit:
            await set_value(HALT_KEY, "TRUE")
            logger.critical(
                "Daily drawdown limit breached: ₹%.2f >= ₹%.2f — HALTING",
                total_loss, limit,
            )
            await send_halt_alert()
            await publish("system_alerts", {
                "type": "critical",
                "message": (
                    f"Daily drawdown limit breached: ₹{total_loss:.2f} "
                    f"({drawdown_pct:.1f}%) — trading HALTED"
                ),
                "timestamp": datetime.now(IST).isoformat(),
            })

    async def _generate_eod_report(self) -> None:
        """Generate and publish the End of Day report.

        Uses an upsert pattern to handle process restarts: if a DailyPnl row
        for today already exists, it is updated rather than duplicated.
        Reconciles orphaned CLOSING trades before computing the report.
        """
        self._eod_reported_today = True
        today = date.today()

        # ── Reconcile orphaned CLOSING trades ─────────────────────
        # If a process crash left trades in CLOSING state, they'll never
        # transition to CLOSED on their own.  Force-close them at entry
        # price (zero P&L) so the EOD report is accurate.
        async with get_db_context() as session:
            orphan_result = await session.execute(
                select(Trade).where(
                    Trade.trade_date == today,
                    Trade.status == "CLOSING",
                )
            )
            orphaned = orphan_result.scalars().all()
            if orphaned:
                now_ist = datetime.now(IST)
                for t in orphaned:
                    logger.warning(
                        "Reconciling orphaned CLOSING trade: %s (id=%d)", t.stock, t.id,
                    )
                    await session.execute(
                        update(Trade)
                        .where(Trade.id == t.id)
                        .values(
                            status="CLOSED",
                            exit_price=t.entry_price,
                            exit_reason="RECONCILED",
                            exit_time=now_ist.time(),
                            realized_pnl=0.0,
                        )
                    )
                logger.warning("Reconciled %d orphaned CLOSING trades", len(orphaned))

        async with get_db_context() as session:
            result = await session.execute(
                select(Trade).where(Trade.trade_date == today)
            )
            todays_trades = result.scalars().all()

        total = len(todays_trades)
        closed = [t for t in todays_trades if t.status == "CLOSED"]
        won = sum(1 for t in closed if (t.realized_pnl or 0) > 0)
        lost = sum(1 for t in closed if (t.realized_pnl or 0) < 0)
        net_pnl = sum(t.realized_pnl or 0 for t in closed)
        starting = self._settings.total_capital
        return_pct = (net_pnl / starting) * 100 if starting > 0 else 0.0

        # ── Enhanced metrics ──────────────────────
        pnls = [t.realized_pnl for t in closed if t.realized_pnl is not None]
        positive_pnls = [p for p in pnls if p > 0]
        negative_pnls = [p for p in pnls if p < 0]

        # Profit factor = gross profit / gross loss (dimensionless ratio).
        # Convention for zero-loss sessions: report 999.0 ("infinite") rather
        # than gross profit in rupees, which is dimensionally wrong.
        if negative_pnls:
            profit_factor = round(sum(positive_pnls) / abs(sum(negative_pnls)), 2)
        elif positive_pnls:
            profit_factor = 999.0   # all wins — no losses to divide by
        else:
            profit_factor = 0.0     # no closed trades with P&L

        # Average trade duration (minutes)
        durations: list[float] = []
        for t in closed:
            if t.entry_time and t.exit_time:
                entry_dt = datetime.combine(today, t.entry_time)
                exit_dt = datetime.combine(today, t.exit_time)
                durations.append((exit_dt - entry_dt).total_seconds() / 60)
        avg_duration = round(sum(durations) / len(durations), 1) if durations else 0.0

        # Max consecutive losses (ordering by exit time)
        max_consec_losses = 0
        streak = 0
        for t in sorted(closed, key=lambda x: x.exit_time or dt_time(0, 0)):
            if (t.realized_pnl or 0) < 0:
                streak += 1
                max_consec_losses = max(max_consec_losses, streak)
            else:
                streak = 0

        # Simplified Sharpe (per-trade: mean / stdev)
        if len(pnls) > 1:
            mean_pnl = sum(pnls) / len(pnls)
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
            std_pnl = variance ** 0.5
            sharpe = round(mean_pnl / std_pnl, 2) if std_pnl > 0 else 0.0
        else:
            sharpe = 0.0

        halt = await get_value(HALT_KEY)

        # Persist to DB — upsert to survive process restarts
        async with get_db_context() as session:
            existing = await session.execute(
                select(DailyPnl).where(DailyPnl.date == today)
            )
            eod = existing.scalars().first()
            if eod is not None:
                # Update existing row
                eod.starting_capital = starting
                eod.ending_capital = starting + net_pnl
                eod.realized_pnl = net_pnl
                eod.unrealized_pnl = 0.0
                eod.total_trades = total
                eod.winning_trades = won
                eod.losing_trades = lost
                eod.return_pct = return_pct
                eod.trading_halted = (halt == "TRUE")
                session.add(eod)
            else:
                eod = DailyPnl(
                    date=today,
                    starting_capital=starting,
                    ending_capital=starting + net_pnl,
                    realized_pnl=net_pnl,
                    unrealized_pnl=0.0,
                    total_trades=total,
                    winning_trades=won,
                    losing_trades=lost,
                    return_pct=return_pct,
                    trading_halted=(halt == "TRUE"),
                )
                session.add(eod)

        # Publish to Redis
        eod_data = {
            "type": "EOD_REPORT",
            "date": str(today),
            "total_trades": total,
            "won": won,
            "lost": lost,
            "net_pnl": net_pnl,
            "return_pct": return_pct,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "avg_duration_min": avg_duration,
            "max_consecutive_losses": max_consec_losses,
        }
        await publish("eod_report", eod_data)
        await publish("system_alerts", {
            "type": "info",
            "message": (
                f"EOD Report: {total} trades | W{won}/L{lost} | "
                f"P&L ₹{net_pnl:.2f} ({return_pct:.2f}%) | "
                f"PF={profit_factor} Sharpe={sharpe} AvgDur={avg_duration}m"
            ),
            "timestamp": datetime.now(IST).isoformat(),
        })

        # Telegram
        await send_eod_report(total, won, lost, net_pnl, return_pct)

        logger.info(
            "═══ EOD: trades=%d W%d/L%d pnl=₹%.2f (%.2f%%) | "
            "PF=%.2f Sharpe=%.2f AvgDur=%.1fm MaxConsecL=%d ═══",
            total, won, lost, net_pnl, return_pct,
            profit_factor, sharpe, avg_duration, max_consec_losses,
        )
