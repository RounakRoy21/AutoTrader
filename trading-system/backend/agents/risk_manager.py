"""
Risk Manager — Deterministic, rule-based position monitoring.

Runs on a separate thread, polling every 5 seconds. ZERO LLM involvement.
Checks all open positions against stop-loss / target prices, enforces
daily loss limits, and handles intraday square-off at 3:00 PM IST.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, List, Optional

import pytz
from sqlalchemy import select

from core.nse_calendar import ist_today

from core.config import get_settings
from core.database import get_db_context
from core.tick_size import get_tick_size
from core.redis_client import get_value, publish, set_value
from core.redis_keys import (
    RISK_STATE_TTL_S,
    consecutive_losses_key,
    halt_key,
    loss_pause_until_key,
    stock_lock_key,
)
from core.risk_state import set_halt
from integrations.broker import get_broker
from integrations.groww_client import get_groww_client
from integrations import ltp_store
from integrations.telegram_client import (
    send_drawdown_soft_alert,
    send_eod_report,
    send_halt_alert,
    send_intraday_close_alert,
    send_stop_loss_alert,
    send_target_hit_alert,
    send_telegram,
)
from models.daily_pnl import DailyPnl
from models.trade import Trade

from . import exit_policy, order_lifecycle
from .position_service import PositionDeps, PositionService, stop_exit_reason

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
POLL_INTERVAL = 5  # seconds

# Intraday close times
MIS_CLOSE_START = dt_time(15, 0)   # 3:00 PM IST
MIS_FORCE_CLOSE = dt_time(15, 20)  # 3:20 PM IST — escalation for anything still open
EOD_REPORT_TIME = dt_time(15, 30)  # 3:30 PM IST

# A CLOSING row (exit SELL state unknown / crash mid-exit) is re-checked against
# the broker once it has been CLOSING this long (audit R9).  After
# MIS_FORCE_CLOSE it is re-checked on every poll.
CLOSING_RECHECK_AFTER_S = 30.0
# Wall-clock budget for one poll before the run loop logs it as overrunning.
# The in-flight poll is never cancelled (that could interrupt an exit between
# the SELL and its DB write); overlapping polls are prevented instead.
POLL_TIMEOUT_S = 30.0
# Consecutive skipped cycles (previous poll still running) before a critical
# "risk monitoring stalled" alert: 6 × POLL_INTERVAL ≈ 30 s.
POLL_STUCK_ALERT_CYCLES = 6


async def record_close_outcome(stock: str, net_pnl: float, settings, now_ist: datetime) -> None:
    """Update today's per-stock lock and consecutive-loss streak after a close.

    Audit R14: driven by the SIGN of the trade's net realised P&L, never by the
    exit reason — a profitable trailing / breakeven exit is a win, a losing
    manual or EOD exit is a loss.

      * net < 0  → lock the stock for the rest of the day (if enabled) and
                   advance the streak; pause new entries at the threshold.
      * net > 0  → reset the streak.
      * net == 0 → neutral.

    All keys are date-scoped (audit R7 / C8) so they survive restarts and lapse
    on the next trading day.
    """
    if net_pnl < 0:
        if getattr(settings, "stock_lock_after_sl", False):
            await set_value(stock_lock_key(stock), "TRUE", ttl=RISK_STATE_TTL_S)
            logger.info("Stock locked for today after a net loss: %s", stock)
        try:
            count = int(await get_value(consecutive_losses_key()) or "0") + 1
        except (TypeError, ValueError):
            count = 1
        await set_value(consecutive_losses_key(), str(count), ttl=RISK_STATE_TTL_S)
        if count >= settings.consecutive_loss_pause_threshold:
            pause_until = now_ist + timedelta(minutes=settings.consecutive_loss_pause_minutes)
            await set_value(loss_pause_until_key(), pause_until.isoformat(), ttl=RISK_STATE_TTL_S)
            logger.warning(
                "%d consecutive losses — trading paused until %s",
                count, pause_until.strftime("%H:%M"),
            )
    elif net_pnl > 0:
        await set_value(consecutive_losses_key(), "0", ttl=RISK_STATE_TTL_S)


def _transaction_costs(entry_price: float, exit_price: float, qty: int, product_type: str) -> float:
    """Estimate round-trip NSE transaction costs for a single trade.

    Applies NSE broker fee schedule (Zerodha/Groww rates as of 2025):
    - Brokerage:           ₹20/order (capped at 0.03% per leg) — both entry and exit
    - STT:                 0.025% of sell-side turnover (intraday MIS/NRML)
                           0.1%   of both sides (CNC delivery)
    - Exchange charges:    0.00345% of total turnover (NSE equities)
    - GST:                 18% on (brokerage + exchange charges)
    - SEBI turnover fee:   0.0001% of total turnover
    - Stamp duty:          0.003% of buy-side turnover

    Returns the total cost estimate in rupees.  The result is subtracted from
    gross P&L so that ``realized_pnl`` reflects what actually hits the bank.
    """
    buy_value = entry_price * qty
    sell_value = exit_price * qty
    total_turnover = buy_value + sell_value

    # Brokerage: ₹20 flat per executed order, capped at 0.03% of that leg's value
    brokerage = min(20.0, buy_value * 0.0003) + min(20.0, sell_value * 0.0003)

    # STT: intraday (MIS/NRML) → sell side only; delivery (CNC) → both sides
    if product_type.upper() in ("MIS", "NRML"):
        stt = sell_value * 0.00025
    else:
        stt = total_turnover * 0.001

    # Exchange transaction charges (NSE equity segment)
    exchange_charges = total_turnover * 0.0000345

    # GST on brokerage + exchange charges
    gst = (brokerage + exchange_charges) * 0.18

    # SEBI turnover fee
    sebi = total_turnover * 0.000001

    # Stamp duty: 0.003% on buy-side only (rounded up at exchange, approximated here)
    stamp = buy_value * 0.00003

    return round(brokerage + stt + exchange_charges + gst + sebi + stamp, 2)


class RiskManager:
    """
    Deterministic risk manager that monitors all open positions.
    Rules enforced:
      - Stop-loss exit: ATR-based (entry − ATR × multiplier) or fixed % fallback
      - Target exit: ATR-based (entry + ATR × multiplier) or fixed % fallback
      - Trailing stop-loss activates once price moves above activation threshold
      - Partial profit-booking: scale out a fraction at entry + R (move SL to breakeven)
      - ROI decay: target reduced over time (>20m→1.5%, >35m→1.0%, >50m→0.5%/ATR-floor)
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
        self._drawdown_soft_alerted = False
        # Consecutive unusable-LTP counter per symbol (audit R2) and the set of
        # symbols already alerted for the current miss streak.
        self._ltp_miss_counts: Dict[str, int] = {}
        self._ltp_miss_alerted: set = set()
        # Single-flight guard (audit R6 / C4): only one poll may run at a time.
        self._poll_in_flight = False
        self._poll_future = None
        self._poll_skips = 0
        # CLOSING reconciliation (audit R9): first time each CLOSING trade id
        # was seen (monotonic seconds) and the ids already alerted.
        self._closing_seen: Dict[int, float] = {}
        self._closing_alerted: set = set()
        self._force_close_escalated = False

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the risk manager on a separate daemon thread."""
        self._loop = loop
        self._running = True
        now_time = datetime.now(IST).time()
        # Pre-mark as done if we are restarting after the cut-off times so a
        # restart post-3:30 PM doesn't re-fire the EOD report or intraday
        # square-off alert on every subsequent poll cycle.
        self._eod_reported_today = now_time >= EOD_REPORT_TIME
        self._intraday_close_initiated = now_time >= MIS_CLOSE_START
        self._drawdown_soft_alerted = False
        self._force_close_escalated = False
        # A poll left hung by a previous session must not block this one forever.
        prev = getattr(self, "_poll_future", None)
        if prev is not None and not prev.done():
            logger.error("Risk Manager restart: previous poll still hung — no longer waiting for it")
        self._poll_in_flight = False
        self._poll_future = None
        self._poll_skips = 0
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
        """Main polling loop — runs every POLL_INTERVAL seconds.

        Non-reentrant (audit R6 / C4): a poll that overruns ``POLL_TIMEOUT_S``
        is NOT cancelled (cancelling could interrupt an exit between its SELL
        and its DB write) and no new poll is submitted until it finishes.
        ``_poll_once`` additionally refuses to start while one is in flight.
        """
        while self._running:
            try:
                prev = self._poll_future
                if prev is not None and not prev.done():
                    self._poll_skips += 1
                    logger.warning(
                        "Risk Manager: previous poll still running — skipping this cycle (%d)",
                        self._poll_skips,
                    )
                    if self._poll_skips == POLL_STUCK_ALERT_CYCLES:
                        asyncio.run_coroutine_threadsafe(
                            self._alert_poll_stuck(self._poll_skips * POLL_INTERVAL), self._loop,
                        )
                else:
                    self._poll_skips = 0
                    self._poll_future = asyncio.run_coroutine_threadsafe(self._poll_once(), self._loop)
                    self._poll_future.result(timeout=POLL_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                logger.error(
                    "Risk Manager poll exceeded %.0fs — left running; next poll waits for it",
                    POLL_TIMEOUT_S,
                )
            except Exception as exc:
                logger.exception("Risk Manager poll error: %s", exc)
            time.sleep(POLL_INTERVAL)

    async def _alert_poll_stuck(self, stalled_s: float) -> None:
        """Critical alert: one poll has been running so long that SL / target /
        square-off / drawdown monitoring is stalled."""
        msg = (
            f"Risk Manager poll STALLED for ~{stalled_s:.0f}s (a broker call is hanging). "
            f"Client-side SL / target / square-off monitoring is NOT running — "
            f"check open positions in Groww now."
        )
        logger.critical(msg)
        try:
            await publish("system_alerts", {
                "type": "critical",
                "message": msg,
                "timestamp": datetime.now(IST).isoformat(),
            })
            await send_telegram(f"\U0001f6a8 {msg}")
        except Exception as exc:
            logger.warning("Poll-stalled alert failed: %s", exc)

    async def _poll_once(self) -> bool:
        """Run one poll unless another is already in flight.  Returns True if it ran."""
        if self._poll_in_flight:
            logger.warning("Risk Manager poll already in flight — overlapping poll skipped")
            return False
        self._poll_in_flight = True
        try:
            await self._poll()
        finally:
            self._poll_in_flight = False
        return True

    def _paper_fallback_price(self, trade: Trade) -> float:
        """Read paper LTP from scanner tick store, falling back to entry price."""
        price = ltp_store.get_ltp(trade.stock)
        if price is None:
            price = trade.entry_price
            logger.debug(
                "[RiskManager] No tick yet for %s — using entry price",
                trade.stock,
            )
        return price

    def _validate_broker_ltp(self, stock: str, price) -> Optional[float]:
        """Return a usable broker LTP or None (audit R2).

        Rejects missing / non-numeric / non-positive prices and prices more
        than ``risk_ltp_sanity_band_pct`` away from the latest WebSocket tick
        in ``ltp_store`` (when a tick is available).  A rejected price must
        never drive an exit.
        """
        try:
            ltp = float(price)
        except (TypeError, ValueError):
            return None
        if ltp != ltp or ltp <= 0:
            return None
        band = getattr(self._settings, "risk_ltp_sanity_band_pct", 0.05)
        try:
            ws_tick = ltp_store.get_ltp(stock)
        except Exception:
            ws_tick = None
        if ws_tick and ws_tick > 0 and band and abs(ltp - ws_tick) / ws_tick > band:
            logger.warning(
                "[RiskManager] Implausible LTP for %s: broker ₹%.2f vs WS tick ₹%.2f "
                "(> %.1f%% apart) — ignored",
                stock, ltp, ws_tick, band * 100,
            )
            return None
        return ltp

    def _record_ltp_ok(self, stock: str) -> None:
        self._ltp_miss_counts.pop(stock, None)
        self._ltp_miss_alerted.discard(stock)

    async def _record_ltp_miss(self, stock: str) -> None:
        """Count a skipped poll for *stock*; alert once after N consecutive misses."""
        count = self._ltp_miss_counts.get(stock, 0) + 1
        self._ltp_miss_counts[stock] = count
        threshold = getattr(self._settings, "risk_ltp_miss_alert_threshold", 3)
        if count >= threshold and stock not in self._ltp_miss_alerted:
            self._ltp_miss_alerted.add(stock)
            logger.error(
                "[RiskManager] No usable LTP for %s for %d consecutive polls — "
                "position NOT being monitored client-side (broker SL still resting)",
                stock, count,
            )
            try:
                await send_telegram(
                    f"\u26a0\ufe0f No usable price for {stock} for {count} consecutive "
                    f"risk polls — client-side SL/target checks are paused. "
                    f"Check the position manually."
                )
            except Exception as exc:
                logger.warning("LTP-miss Telegram alert failed: %s", exc)

    async def _build_ltp_map(self, open_trades: List[Trade]) -> Dict[str, float]:
        """Build symbol → LTP mapping for the current poll cycle.

        Live mode always uses broker LTP.
        Paper mode uses broker LTP when enabled and falls back per symbol to
        scanner ticks (or entry price when no tick is available).

        Every broker price is validated (``_validate_broker_ltp``).  In live
        mode a symbol without a usable price is left OUT of the map, so the
        poll skips it instead of exiting on a bogus value (audit R2).
        """
        settings = self._settings
        use_broker = (not settings.paper_trading) or settings.paper_risk_use_broker_ltp
        ltp_map: Dict[str, float] = {}
        raw: Dict[str, Dict[str, float]] = {}

        if use_broker:
            try:
                groww = get_groww_client()
                instruments = [f"NSE:{t.stock}" for t in open_trades]
                raw = await groww.get_ltp(instruments) or {}
            except Exception as exc:
                if not settings.paper_trading:
                    logger.error("Failed to fetch LTP for risk check: %s", exc)
                    for t in open_trades:
                        await self._record_ltp_miss(t.stock)
                    raise
                logger.warning(
                    "Paper broker LTP fetch failed for risk check: %s — falling back to scanner ticks",
                    exc,
                )

        for trade in open_trades:
            key = f"NSE:{trade.stock}"
            ltp = None
            if key in raw:
                ltp = self._validate_broker_ltp(trade.stock, (raw[key] or {}).get("last_price"))
            if ltp is not None:
                ltp_map[trade.stock] = ltp
                self._record_ltp_ok(trade.stock)
                continue

            if settings.paper_trading:
                fallback = self._paper_fallback_price(trade)
                if fallback is not None and fallback > 0:
                    ltp_map[trade.stock] = fallback
                    continue

            await self._record_ltp_miss(trade.stock)

        return ltp_map

    async def _get_trade_ltp(self, trade: Trade) -> Optional[float]:
        """Get a single-trade LTP with the same source preference as main poll.

        Returns None (never 0.0) when no usable price is available.
        """
        settings = self._settings
        use_broker = (not settings.paper_trading) or settings.paper_risk_use_broker_ltp

        if use_broker:
            try:
                groww = get_groww_client()
                raw = await groww.get_ltp([f"NSE:{trade.stock}"])
                ltp = self._validate_broker_ltp(
                    trade.stock, (raw or {}).get(f"NSE:{trade.stock}", {}).get("last_price")
                )
                if ltp is not None:
                    return ltp
            except Exception:
                # Keep existing live-mode behavior: silently continue to fallback.
                pass

        if settings.paper_trading:
            price = ltp_store.get_ltp(trade.stock)
            return price if price is not None and price > 0 else None

        return None

    async def _poll(self) -> None:
        """Single poll cycle: check positions, enforce rules."""
        now_ist = datetime.now(IST)
        current_time = now_ist.time()

        # Skip outside market hours
        if current_time < dt_time(9, 15) or current_time > dt_time(15, 35):
            return

        # Fetch OPEN and CLOSING positions from DB.  CLOSING rows stay
        # monitored (audit R9) and are reconciled against the broker below.
        managed = await self._get_open_trades()
        open_trades = [t for t in managed if getattr(t, "status", "OPEN") == "OPEN"]
        closing_trades = [t for t in managed if getattr(t, "status", "OPEN") == "CLOSING"]
        if not managed:
            self._closing_seen.clear()
            # Re-assert the drawdown halt even with no open positions so a
            # restart can never let a new entry through first (audit C8).
            await self._check_daily_drawdown({})
            # Check if it's EOD report time
            if current_time >= EOD_REPORT_TIME and not self._eod_reported_today:
                await self._generate_eod_report()
            return

        # Build a symbol → LTP mapping:
        #   • Live mode   → Groww REST LTP (real market data)
        #   • Paper mode  → Groww REST LTP (optional), then tick-store fallback
        settings = self._settings
        ltp_ok = True
        try:
            ltp_map = await self._build_ltp_map(managed)
        except Exception:
            # No prices this cycle.  SL / target / trailing are skipped, but the
            # MIS square-off and CLOSING reconciliation still run: a market
            # exit does not need an LTP (audit R18).
            ltp_map = {}
            ltp_ok = False
            if current_time < MIS_CLOSE_START and not closing_trades:
                return

        if closing_trades:
            await self._reconcile_closing_trades(closing_trades, ltp_map, now_ist)

        for trade in open_trades:
            ltp = ltp_map.get(trade.stock)
            square_off_due = exit_policy.square_off_due(trade, current_time)
            # Never act on a missing / non-positive price (audit R2) — except
            # the MIS square-off, which exits at market (audit R18).
            if ltp is None or ltp <= 0:
                if square_off_due:
                    await self._square_off(trade, None, open_trades, ltp_map, now_ist)
                continue

            # ── Stop Loss Check ────────────────────
            # The exit reason reflects how the stop was last moved (audit R14):
            # STOP_LOSS_HIT (initial), TRAILING_SL or BREAKEVEN.
            if exit_policy.stop_hit(trade, ltp):
                sl_reason = stop_exit_reason(trade)
                if not await self._close_position(trade, ltp, sl_reason):
                    continue  # exit not confirmed — still managed / alerted
                exit_reason = getattr(trade, "exit_reason", None) or sl_reason
                ltp = getattr(trade, "exit_price", None) or ltp
                move = (ltp - trade.entry_price) * trade.quantity
                if exit_reason == "STOP_LOSS_HIT" or move < 0:
                    loss = -move
                    await send_stop_loss_alert(
                        trade.stock, ltp, loss,
                        entry_price=trade.entry_price,
                        entry_time=trade.entry_time,
                        quantity=trade.quantity,
                        paper=settings.paper_trading,
                    )
                    await publish("system_alerts", {
                        "type": "danger",
                        "message": f"Stop-loss hit ({exit_reason}): {trade.stock} @ ₹{ltp:.2f} | Loss: ₹{loss:.2f}",
                        "timestamp": now_ist.isoformat(),
                    })
                else:
                    label = "Trailing stop" if exit_reason == "TRAILING_SL" else "Breakeven stop"
                    await send_telegram(
                        f"✅ {label} exit: {trade.stock} @ ₹{ltp:.2f} | P&L: ₹{move:.2f}"
                        + (" (paper)" if settings.paper_trading else "")
                    )
                    await publish("system_alerts", {
                        "type": "success",
                        "message": f"{label} exit: {trade.stock} @ ₹{ltp:.2f} | P&L: ₹{move:.2f}",
                        "timestamp": now_ist.isoformat(),
                    })
                continue

            # ── Partial Profit-Booking / Scale-Out ──
            # Evaluated AFTER the SL check (a position at its stop exits in full,
            # never scales out) and BEFORE ROI-decay / target so the booked leg is
            # locked in on the way up.  Deliberately does NOT `continue`: the
            # remaining position is re-evaluated for trailing-SL / target in the
            # same cycle.  `_book_partial` reduces `trade.quantity` in place, so
            # every downstream rule (target, trailing, MTM, drawdown, final close)
            # automatically operates on the remainder.
            if exit_policy.partial_due(trade, ltp, settings):
                await self._book_partial(trade, ltp)
                if getattr(trade, "status", "OPEN") != "OPEN":
                    continue  # the scale-out closed / parked the trade

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
                # ATR (stored at entry) only matters for the > 50 min floor.
                atr_val = None
                if exit_policy.minutes_in_trade(trade, now_ist) > 50:
                    try:
                        atr_str = await get_value(f"trade_atr:{trade.stock}")
                        atr_val = float(atr_str) if atr_str else None
                    except Exception:
                        atr_val = None  # non-critical — 0.5% floor applies
                decayed = exit_policy.roi_decay_target(
                    trade, now_ist, atr_val, get_tick_size(trade.stock),
                )
                if decayed is not None:
                    old_tgt = trade.target_price
                    await self._update_target(trade, decayed)
                    logger.info(
                        "ROI decay: %s target ₹%.2f → ₹%.2f (%.0f min elapsed)",
                        trade.stock, old_tgt, decayed, exit_policy.minutes_in_trade(trade, now_ist),
                    )

            # ── Target Check ───────────────────────
            # Runs AFTER ROI decay so the possibly-reduced target is used.
            if exit_policy.target_hit(trade, ltp):
                if not await self._close_position(trade, ltp, "TARGET_HIT"):
                    continue  # exit not confirmed — still managed / alerted
                if getattr(trade, "exit_reason", None) not in (None, "TARGET_HIT"):
                    continue  # the broker stop had already executed instead
                ltp = getattr(trade, "exit_price", None) or ltp
                profit = (ltp - trade.entry_price) * trade.quantity
                await send_target_hit_alert(
                    trade.stock, ltp, profit,
                    entry_price=trade.entry_price,
                    entry_time=trade.entry_time,
                    quantity=trade.quantity,
                    paper=settings.paper_trading,
                )
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
            trailing_sl = exit_policy.trailing_stop(trade, ltp, settings, get_tick_size(trade.stock))
            if trailing_sl is not None:
                old_sl = trade.stop_loss_price
                await self._update_stop_loss(trade, trailing_sl, kind="TRAILING")
                logger.info(
                    "Trailing SL: %s ₹%.2f → ₹%.2f (LTP=₹%.2f)",
                    trade.stock, old_sl, trailing_sl, ltp,
                )

            # ── MIS Intraday Close (3:00 PM) ──────
            # Retried on EVERY poll until the position is flat (audit R18): a
            # failed / unfilled exit reverts the row to OPEN, so the next cycle
            # lands here again.
            if square_off_due:
                await self._square_off(trade, ltp, open_trades, ltp_map, now_ist)
                continue

        # ── 15:20 escalation for anything still open / closing ──
        if current_time >= MIS_FORCE_CLOSE:
            await self._escalate_force_close(now_ist)

        # ── Daily Drawdown Halt ───────────────────
        if ltp_ok:
            await self._check_daily_drawdown(ltp_map)

        # ── EOD Report ────────────────────────────
        if current_time >= EOD_REPORT_TIME and not self._eod_reported_today:
            await self._generate_eod_report()

    async def _square_off(
        self,
        trade: Trade,
        ltp: Optional[float],
        open_trades: List[Trade],
        ltp_map: Dict[str, float],
        now_ist: datetime,
    ) -> bool:
        """MIS square-off exit for one position (from 15:00, every poll until flat).

        Does not need an LTP (audit R18): live exits are booked from the broker
        fill; *ltp* is only the fallback booking estimate.
        """
        settings = self._settings
        if not self._intraday_close_initiated:
            mis_count = sum(1 for t in open_trades if t.product_type == "MIS")
            running_pnl = sum(
                (ltp_map.get(t.stock, t.entry_price) - t.entry_price) * t.quantity
                for t in open_trades
            )
            await send_intraday_close_alert(n_positions=mis_count, running_pnl=running_pnl, paper=settings.paper_trading)
            await publish("system_alerts", {
                "type": "warning",
                "message": "MIS square-off initiated — all intraday positions closing",
                "timestamp": now_ist.isoformat(),
            })
            self._intraday_close_initiated = True
        estimate = ltp
        if estimate is None or estimate <= 0:
            estimate = (
                self._paper_fallback_price(trade) if settings.paper_trading else trade.entry_price
            )
            logger.warning(
                "MIS square-off for %s without a usable LTP — exiting at market anyway", trade.stock,
            )
        return await self._close_position(trade, estimate, "EOD_CLOSE")

    async def _escalate_force_close(self, now_ist: datetime) -> None:
        """At/after MIS_FORCE_CLOSE (15:20): loudly escalate anything still not flat.

        The square-off and CLOSING reconciliation keep retrying every poll; this
        makes sure a human knows before the broker's own auto-square-off.
        """
        if self._force_close_escalated:
            return
        remaining = await self._get_open_trades()
        if not remaining:
            return
        self._force_close_escalated = True
        names = [f"{t.stock}#{t.id}({getattr(t, 'status', 'OPEN')})" for t in remaining]
        msg = (
            f"{len(remaining)} position(s) still not flat at "
            f"{MIS_FORCE_CLOSE.strftime('%H:%M')}: {names}. Exits are being retried every poll — "
            f"CHECK GROWW NOW before the broker auto-square-off."
        )
        logger.critical("MIS force-close escalation: %s", msg)
        try:
            await publish("system_alerts", {
                "type": "critical",
                "message": msg,
                "timestamp": now_ist.isoformat(),
            })
            await send_telegram(f"\U0001f6a8 {msg}")
        except Exception as exc:
            logger.warning("Force-close escalation alert failed: %s", exc)

    # ── CLOSING reconciliation (audit R9) ─────────────────────────────────────

    async def _reconcile_closing_trades(
        self, closing: List[Trade], ltp_map: Dict[str, float], now_ist: datetime,
    ) -> None:
        """Re-check CLOSING rows against the broker instead of abandoning them."""
        live_ids = {t.id for t in closing}
        for tid in list(self._closing_seen):
            if tid not in live_ids:
                self._closing_seen.pop(tid, None)
                self._closing_alerted.discard(tid)
        now_mono = time.monotonic()
        escalate = now_ist.time() >= MIS_FORCE_CLOSE
        for trade in closing:
            first = self._closing_seen.setdefault(trade.id, now_mono)
            if order_lifecycle.is_exit_in_flight(trade.id):
                # Another closer (manual-close API / this manager) still owns
                # the exit — even after 15:20 it must not be second-guessed.
                self._closing_seen[trade.id] = now_mono
                continue
            if not escalate and now_mono - first < CLOSING_RECHECK_AFTER_S:
                continue
            try:
                await self._reconcile_closing(trade, ltp_map.get(trade.stock), now_ist)
            except Exception as exc:
                logger.error("CLOSING reconciliation failed for %s: %s", trade.stock, exc)

    async def _alert_closing_once(self, trade: Trade, detail: str, critical: bool = True) -> None:
        if trade.id in self._closing_alerted:
            return
        self._closing_alerted.add(trade.id)
        await self._alert_exit_problem(trade, "CLOSING", detail, critical=critical)

    async def _reconcile_closing(
        self, trade: Trade, ltp: Optional[float], now_ist: datetime,
    ) -> None:
        """Resolve one CLOSING row against the broker (paper: the PaperBroker).

        * Broker still holds shares and no SELL is working: the exit never
          executed — re-place the stop and return the row to OPEN (the normal
          exit logic retries it).  A SELL still working → wait.
        * Broker flat: the exit DID execute — book it from the broker fill
          (resting-stop fill, else the order book's last SELL fill, else an LTP
          estimate flagged for verification).  Never booked at entry / zero P&L.
        """
        groww = get_broker(self._settings)
        held = await order_lifecycle.broker_net_qty(groww, trade.stock)
        if held is None:
            await self._alert_closing_once(
                trade, "broker positions unreadable — CLOSING row not reconciled yet (retrying)",
            )
            return

        if held > 0:
            own_sl = getattr(trade, "sl_order_id", None)
            pending = await order_lifecycle.has_pending_sell(
                groww, trade.stock, exclude_order_id=own_sl,
            )
            if pending is None or pending:
                await self._alert_closing_once(
                    trade,
                    "broker still holds shares and an exit SELL is still working (or the order "
                    "book is unreadable) — waiting before any retry",
                )
                return
            if own_sl and not order_lifecycle.is_unresolved_stop(own_sl):
                own = await order_lifecycle.read_order(groww, str(own_sl))
                if own is None:
                    await self._alert_closing_once(
                        trade, "the row's broker stop state is unreadable — waiting before any retry",
                    )
                    return
                if not own.terminal:
                    # Crash between the CLOSING claim and the stop release: the
                    # original stop still protects the shares — keep it.
                    await self._revert_to_open(trade)
                    self._closing_seen.pop(trade.id, None)
                    self._closing_alerted.discard(trade.id)
                    await self._alert_exit_problem(
                        trade, "CLOSING",
                        f"exit did NOT execute at the broker ({held} still held) — position back "
                        f"to OPEN under its resting broker stop; exit will be retried",
                    )
                    return
            # Release whatever the row still points at (a terminal stop, or an
            # ambiguous ``ref:`` placement) before placing a fresh one, so the
            # broker never holds two stops for the same shares.
            release = await order_lifecycle.release_stop(groww, trade)
            if release.outcome == "UNCONFIRMED":
                await self._alert_closing_once(
                    trade, "the row's broker stop could not be confirmed cancelled — waiting",
                )
                return
            if release.filled_qty:
                after = await order_lifecycle.broker_net_qty(groww, trade.stock)
                held = after if after is not None else max(0, held - int(release.filled_qty))
        if held > 0:
            qty = min(int(trade.quantity), int(held))
            sl_id = await order_lifecycle.place_stop(
                groww, trade.stock, getattr(trade, "exchange", None) or "NSE", qty, trade.stop_loss_price,
            )
            await self._revert_to_open(trade, sl_order_id=sl_id)
            self._closing_seen.pop(trade.id, None)
            self._closing_alerted.discard(trade.id)
            stop_ok = bool(sl_id) and not order_lifecycle.is_unresolved_stop(sl_id)
            await self._alert_exit_problem(
                trade, "CLOSING",
                f"exit did NOT execute at the broker ({held} still held) — position back to OPEN"
                + (" with a fresh broker stop; exit will be retried" if stop_ok else
                   " with NO confirmed broker stop — exit will be retried"),
                critical=not stop_ok,
            )
            return

        if held < 0:
            await set_halt(f"net short {held} in {trade.stock} found while reconciling a CLOSING row")
            await self._alert_exit_problem(
                trade, "CLOSING",
                f"broker shows a NET SHORT of {held} — trading HALTED, flatten manually",
                critical=True,
            )

        # Flat at the broker: the exit executed.  Cancel any stale resting stop
        # (if it EXECUTED, its fill is the exit).
        release = await order_lifecycle.release_stop(groww, trade)
        if release.outcome == "UNCONFIRMED":
            await self._alert_closing_once(
                trade, "flat at the broker but the resting stop's state is unconfirmed — not booked yet",
            )
            return
        price: Optional[float] = None
        reason = "RECONCILED"
        source = "broker order book"
        if release.outcome == "EXECUTED" and release.average_price:
            price = float(release.average_price)
            reason = stop_exit_reason(trade)
            source = "broker stop fill"
        if price is None:
            price = await order_lifecycle.last_sell_fill_price(groww, trade.stock)
        estimated = False
        if price is None and ltp:
            price, estimated, source = float(ltp), True, "LTP ESTIMATE"
        if price is None:
            await self._alert_closing_once(
                trade, "flat at the broker but no fill price is available — left CLOSING for manual booking",
            )
            return

        if await self._finalise_close(trade, price, reason, int(trade.quantity), now_ist):
            self._closing_seen.pop(trade.id, None)
            self._closing_alerted.discard(trade.id)
            await self._alert_exit_problem(
                trade, "CLOSING",
                f"reconciled from {source}: booked CLOSED @ ₹{price:.2f} ({reason})"
                + (" — P&L ESTIMATED, verify against the Groww trade book" if estimated else ""),
                critical=estimated,
            )

    async def _get_open_trades(self) -> List[Trade]:
        """Fetch today's OPEN and CLOSING trades from the database.

        CLOSING rows are included so they stay monitored until the broker
        confirms the exit (audit R9); callers split them by ``status``.

        Scoped to today to prevent cross-session unrealised MTM from stale
        paper trades (status=OPEN, trade_date<today) triggering a false
        daily-drawdown halt on every session restart.
        """
        today = ist_today()
        async with get_db_context() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.status.in_(["OPEN", "CLOSING"]),
                    Trade.trade_date == today,
                )
            )
            return list(result.scalars().all())

    # ── Position state changes: all owned by PositionService ────────────────

    def _positions(self) -> PositionService:
        """PositionService wired to this module's IO (resolved per call)."""
        return PositionService(self._settings, PositionDeps(
            db_context=get_db_context,
            broker=lambda: get_broker(self._settings),
            publish=publish,
            notify=send_telegram,
            costs=_transaction_costs,
            record_outcome=record_close_outcome,
        ))

    async def _close_position(
        self,
        trade: Trade,
        exit_price: float,
        reason: str,
        exit_result: Optional[order_lifecycle.ExitResult] = None,
    ) -> bool:
        """Close a position (see ``PositionService.close``).  True only when
        the trade was finalised CLOSED."""
        return bool(await self._positions().close(trade, exit_price, reason, exit_result))

    async def _finalise_close(
        self,
        trade: Trade,
        exit_price: float,
        reason: str,
        qty_closed: int,
        now_ist: datetime,
    ) -> bool:
        return await self._positions().finalise_close(trade, exit_price, reason, qty_closed, now_ist)

    async def _book_partial(self, trade: Trade, ltp: float) -> None:
        """Scale out part of the position (see ``PositionService.book_partial``)."""
        await self._positions().book_partial(trade, ltp)

    # ── Exit-lifecycle DB / alert helpers (audit R1 / R4) ──────────────────────

    _KEEP = object()

    async def _revert_to_open(self, trade: Trade, sl_order_id=_KEEP) -> None:
        """Return a CLOSING trade to OPEN so the poll keeps managing it."""
        if sl_order_id is RiskManager._KEEP:
            await self._positions().revert_to_open(trade)
        else:
            await self._positions().revert_to_open(trade, sl_order_id=sl_order_id)

    async def _alert_exit_problem(
        self, trade: Trade, reason: str, detail: str, critical: bool = False,
    ) -> None:
        await self._positions().alert_exit_problem(trade, reason, detail, critical=critical)

    async def _update_stop_loss(self, trade: Trade, new_sl: float, kind: str = "TRAILING") -> None:
        """Raise the stop in the DB and at the broker (see ``PositionService.update_stop_loss``)."""
        await self._positions().update_stop_loss(trade, new_sl, kind=kind)

    async def _update_target(self, trade: Trade, new_target: float) -> None:
        """Lower the target (ROI decay)."""
        await self._positions().update_target(trade, new_target)

    async def _check_daily_drawdown(self, ltp_map: Optional[Dict[str, float]] = None) -> None:
        """If total daily loss (realised + unrealised) exceeds 3% of capital, halt trading.

        Accepts an optional *ltp_map* (symbol → price) from the main poll cycle
        to avoid redundant Groww API calls.  Falls back to ltp_store / Groww when
        the map is not provided or the symbol is missing.
        """
        halt = await get_value(halt_key())
        if halt == "TRUE":
            return  # already halted

        today = ist_today()

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
        for trade in open_trades:
            # Prefer the ltp_map already fetched in the main poll cycle
            ltp = (ltp_map or {}).get(trade.stock)
            if ltp is None:
                ltp = await self._get_trade_ltp(trade)
            if ltp is None:
                ltp = trade.entry_price
            mtm = (ltp - trade.entry_price) * trade.quantity
            if mtm < 0:
                unrealised_loss += abs(mtm)

        total_loss = total_realised_loss + unrealised_loss
        limit = self._settings.daily_drawdown_limit

        # Always track current drawdown in Redis so the dashboard can read it.
        # TTL = 28 hours so stale values from a prior trading session never
        # bleed into the next day when the agent hasn't started yet.
        drawdown_pct = round((total_loss / limit) * 100, 2) if limit > 0 else 0.0
        await set_value("agent:risk:daily_loss", str(round(total_loss, 2)), ttl=100800)
        await set_value("agent:risk:drawdown_pct", str(drawdown_pct), ttl=100800)

        # ── Soft alert at 2% (warn once — no halt) ────────────────────────────
        soft_limit = self._settings.total_capital * self._settings.daily_drawdown_soft_alert_pct
        if not self._drawdown_soft_alerted and total_loss >= soft_limit:
            self._drawdown_soft_alerted = True
            logger.warning(
                "Drawdown soft alert: ₹%.2f >= ₹%.2f (%.0f%% of capital) — warning sent",
                total_loss, soft_limit, self._settings.daily_drawdown_soft_alert_pct * 100,
            )
            await send_drawdown_soft_alert(
                current_loss=total_loss,
                soft_pct=self._settings.daily_drawdown_soft_alert_pct,
                paper=self._settings.paper_trading,
            )
            await publish("system_alerts", {
                "type": "warning",
                "message": (
                    f"Drawdown warning: ₹{total_loss:.2f} "
                    f"({self._settings.daily_drawdown_soft_alert_pct * 100:.0f}% of capital) "
                    f"— monitor closely"
                ),
                "timestamp": datetime.now(IST).isoformat(),
            })

        if total_loss >= limit:
            await set_halt(f"daily drawdown ₹{total_loss:.2f} >= ₹{limit:.2f}")
            logger.critical(
                "Daily drawdown limit breached: ₹%.2f >= ₹%.2f — HALTING",
                total_loss, limit,
            )
            await send_halt_alert(total_loss=total_loss, drawdown_pct=drawdown_pct)
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
        OPEN / CLOSING rows are alerted, never force-booked (audit R9).
        """
        self._eod_reported_today = True
        today = ist_today()

        # ── Unresolved positions (audit R9) ────────────────────────
        # CLOSING / OPEN rows at 15:30 are NOT booked here (never at entry
        # price / zero P&L): the broker has not confirmed them flat.  They stay
        # for broker reconciliation (``_reconcile_closing`` / next startup's
        # state recovery) and are alerted loudly.
        async with get_db_context() as session:
            unresolved_result = await session.execute(
                select(Trade).where(
                    Trade.trade_date == today,
                    Trade.status.in_(["PENDING", "OPEN", "CLOSING"]),
                )
            )
            unresolved = list(unresolved_result.scalars().all())
        if unresolved:
            names = [f"{t.stock}#{t.id}({t.status})" for t in unresolved]
            msg = (
                f"EOD: {len(unresolved)} position(s) NOT confirmed flat: {names}. "
                f"Not booked — excluded from today's P&L until reconciled against Groww. "
                f"CHECK THE GROWW POSITIONS / TRADE BOOK NOW."
            )
            logger.critical(msg)
            try:
                await publish("system_alerts", {
                    "type": "critical",
                    "message": msg,
                    "timestamp": datetime.now(IST).isoformat(),
                })
                await send_telegram(f"\U0001f6a8 {msg}")
            except Exception as exc:
                logger.warning("EOD unresolved-position alert failed: %s", exc)

        async with get_db_context() as session:
            result = await session.execute(
                select(Trade).where(Trade.trade_date == today, Trade.status != "REJECTED")
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

        # Average realised R:R = avg(winning P&L) / avg(abs(losing P&L))
        if positive_pnls and negative_pnls:
            avg_win = sum(positive_pnls) / len(positive_pnls)
            avg_loss = abs(sum(negative_pnls) / len(negative_pnls))
            avg_realised_rr = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0
        elif positive_pnls:
            avg_realised_rr = 999.0
        else:
            avg_realised_rr = 0.0

        # Loss time distribution — bucket SL exits by session phase
        losses_before_1030 = 0
        losses_1030_to_1330 = 0
        losses_after_1330 = 0
        for t in closed:
            if (t.realized_pnl or 0) < 0 and t.exit_time:
                if t.exit_time < dt_time(10, 30):
                    losses_before_1030 += 1
                elif t.exit_time < dt_time(13, 30):
                    losses_1030_to_1330 += 1
                else:
                    losses_after_1330 += 1

        halt = await get_value(halt_key())

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
                eod.profit_factor = profit_factor
                eod.sharpe_ratio = sharpe
                eod.avg_trade_duration_min = avg_duration
                eod.max_consecutive_losses = max_consec_losses
                eod.avg_realised_rr = avg_realised_rr
                eod.losses_before_1030 = losses_before_1030
                eod.losses_1030_to_1330 = losses_1030_to_1330
                eod.losses_after_1330 = losses_after_1330
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
                    profit_factor=profit_factor,
                    sharpe_ratio=sharpe,
                    avg_trade_duration_min=avg_duration,
                    max_consecutive_losses=max_consec_losses,
                    avg_realised_rr=avg_realised_rr,
                    losses_before_1030=losses_before_1030,
                    losses_1030_to_1330=losses_1030_to_1330,
                    losses_after_1330=losses_after_1330,
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
            "avg_realised_rr": avg_realised_rr,
            "avg_duration_min": avg_duration,
            "max_consecutive_losses": max_consec_losses,
            "losses_before_1030": losses_before_1030,
            "losses_1030_to_1330": losses_1030_to_1330,
            "losses_after_1330": losses_after_1330,
        }
        await publish("eod_report", eod_data)
        await publish("system_alerts", {
            "type": "info",
            "message": (
                f"EOD Report: {total} trades | W{won}/L{lost} | "
                f"P&L ₹{net_pnl:.2f} ({return_pct:.2f}%) | "
                f"PF={profit_factor} R:R={avg_realised_rr} Sharpe={sharpe} AvgDur={avg_duration}m"
            ),
            "timestamp": datetime.now(IST).isoformat(),
        })

        # Telegram
        await send_eod_report(
            total, won, lost, net_pnl, return_pct,
            losses_before_1030, losses_1030_to_1330, losses_after_1330,
            profit_factor=profit_factor,
            sharpe=sharpe,
            avg_realised_rr=avg_realised_rr,
            avg_duration=avg_duration,
            max_consec_losses=max_consec_losses,
            halted=(halt == "TRUE"),
        )

        logger.info(
            "═══ EOD: trades=%d W%d/L%d pnl=₹%.2f (%.2f%%) | "
            "PF=%.2f R:R=%.2f Sharpe=%.2f AvgDur=%.1fm MaxConsecL=%d | "
            "Losses: pre-10:30=%d mid=%d late=%d ═══",
            total, won, lost, net_pnl, return_pct,
            profit_factor, avg_realised_rr, sharpe, avg_duration, max_consec_losses,
            losses_before_1030, losses_1030_to_1330, losses_after_1330,
        )
