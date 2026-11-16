"""
Trading Agent — Orchestrates Scanner, Decision Engine, and Risk Manager.

Activates at 9:15 AM IST, deactivates at 3:30 PM IST.
On startup, runs state recovery to reconstruct open positions from Groww.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import pytz
from sqlalchemy import select, update

from core.config import get_settings
from core.database import get_db_context
from core.redis_client import get_value, increment, publish, set_value, subscribe
from core.redis_keys import DAILY_TRADE_COUNT_KEY
from core.risk_state import is_halted_strict, set_halt
from integrations import ltp_store as ltp_store_module
from core.tick_size import ceil_to_tick, floor_to_tick, get_tick_size
from integrations.broker import get_broker
from integrations.groww_client import OrderStateUnknownError, new_order_reference_id
from integrations.telegram_client import send_telegram, send_trade_entry_alert
from models.trade import Trade
from schemas.decision import DecisionOutput
from schemas.market_brief import MarketBriefLLMOutput
from schemas.trade import ScannerSignal

from . import exit_policy, order_lifecycle
from .decision_engine import DecisionEngine
from .position_service import PositionDeps, PositionService
from .reconciler import Reconciler
from .risk_manager import RiskManager, _transaction_costs, record_close_outcome
from .scanner import Scanner

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
TRADE_COUNT_KEY = DAILY_TRADE_COUNT_KEY  # alias kept for internal clarity


class TradingAgent:
    """
    Top-level orchestrator for the Trading Agent.
    Manages lifecycle of Scanner, Decision Engine, and Risk Manager.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._signal_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._scanner = Scanner(self._signal_queue)
        self._decision_engine = DecisionEngine(self._signal_queue)
        self._risk_manager = RiskManager()
        self._reconciler = Reconciler(self._settings)
        self._running = False

    async def state_recovery(self) -> None:
        """
        MANDATORY first step: reconcile open Groww positions with the DB in
        BOTH directions (audit R8 / C7).

        Safety invariant: the Risk Manager queries ONLY the DB for open trades.

        * Broker MIS LONG with no DB row (crash between fill and insert): a
          synthetic OPEN MIS BUY row is created so the Risk Manager manages it.
          SL / target are estimated from configured percentages (SL floored,
          target ceiled to the tick); the rationale is "RECOVERED — no DB
          record found on startup".  The row is given a broker stop: an SL SELL
          already working for the symbol (placed before the crash) is adopted
          and any extra ones cancelled; otherwise a new stop is placed.  If no
          stop can be confirmed, trading is HALTED.
        * Non-MIS (e.g. CNC delivery) positions are never adopted: they are
          not ours to exit with an MIS SELL.  They are alerted and ignored.
        * Broker SHORT (net qty < 0): this is a long-only system, so a short is
          always an accident (e.g. a duplicate exit).  It is NEVER restored as
          a BUY (exits would deepen it).  Trading is HALTED and a critical
          alert asks the operator to flatten it.
        * DB OPEN row while the broker is flat (e.g. the exchange stop fired
          while the process was down): the row is moved to CLOSING and alerted.
          It is NOT re-sold — the Risk Manager's CLOSING reconciliation books
          it from the broker fill (stop fill / order book) instead.
        """
        logger.info("Running state recovery — reconciling broker positions with DB…")

        # Paper trading: the in-memory PaperBroker lost its state with the
        # process; re-create today's open positions and stops from the DB.
        if self._settings.paper_trading:
            await self._hydrate_paper_broker()
            return

        try:
            groww = get_broker(self._settings)
            positions = await groww.get_positions()
            # Only MIS positions are this system's: a CNC holding in the same
            # account must neither be adopted nor counted against MIS rows.
            net_by_sym: dict = {}
            pos_by_sym: dict = {}
            non_mis: dict = {}
            for p in (positions or {}).get("net", []) or []:
                sym = str(p.get("tradingsymbol") or "")
                if not sym:
                    continue
                try:
                    q = int(p.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                if str(p.get("product") or "MIS").upper() != "MIS":
                    if q != 0:
                        non_mis[sym] = non_mis.get(sym, 0) + q
                    continue
                net_by_sym[sym] = net_by_sym.get(sym, 0) + q
                if q != 0:
                    pos_by_sym.setdefault(sym, p)
        except Exception as exc:
            logger.error(
                "State recovery: Groww positions fetch failed: %s — proceeding with DB state", exc
            )
            await publish("system_alerts", {
                "type": "critical",
                "message": f"State recovery could not read broker positions ({exc}) — "
                           f"positions NOT reconciled. Check Groww manually.",
                "timestamp": datetime.now(IST).isoformat(),
            })
            return

        today = datetime.now(IST).date()
        now_time = datetime.now(IST).time()
        settings = self._settings

        shorts = {sym: q for sym, q in net_by_sym.items() if q < 0}
        longs = {sym: q for sym, q in net_by_sym.items() if q > 0}

        if non_mis:
            detail = ", ".join(f"{s} {q}" for s, q in sorted(non_mis.items()))
            logger.warning("State recovery: ignoring non-MIS broker position(s): %s", detail)
            await publish("system_alerts", {
                "type": "warning",
                "message": f"Non-MIS broker position(s) ignored (not managed by AutoTrader): {detail}",
                "timestamp": datetime.now(IST).isoformat(),
            })

        # ── Net shorts: never adopt as a BUY — halt + alert ──────────────────
        if shorts:
            detail = ", ".join(f"{s} {q}" for s, q in sorted(shorts.items()))
            logger.critical("State recovery: broker holds NET SHORT position(s): %s", detail)
            await set_halt(f"unexpected net short position(s) at broker: {detail}")
            await publish("system_alerts", {
                "type": "critical",
                "message": (
                    f"Broker holds NET SHORT position(s) in a long-only system: {detail}. "
                    f"Trading HALTED — flatten them manually in Groww."
                ),
                "timestamp": datetime.now(IST).isoformat(),
            })
            await send_telegram(
                f"\U0001f6a8 Broker holds NET SHORT position(s): {detail}.\n"
                f"Not adopted. Trading HALTED — flatten manually in Groww now."
            )

        try:
            async with get_db_context() as session:
                result = await session.execute(
                    select(Trade).where(
                        Trade.status.in_(["OPEN", "CLOSING"]),
                        Trade.trade_date == today,
                    )
                )
                db_rows = list(result.scalars().all())
            db_open: dict = {}
            for t in db_rows:
                db_open.setdefault(t.stock, []).append(t)

            # ── Reverse direction: DB OPEN but broker flat (or short) ────────
            # Never re-sell these: park them in CLOSING so the RiskManager
            # books them from the broker fill (audit R8 / R9).
            stale = [
                t for t in db_rows
                if t.status == "OPEN" and net_by_sym.get(t.stock, 0) <= 0
            ]
            if stale:
                async with get_db_context() as session:
                    for t in stale:
                        await session.execute(
                            update(Trade)
                            .where(Trade.id == t.id, Trade.status == "OPEN")
                            .values(status="CLOSING")
                        )
                names = [f"{t.stock}#{t.id}" for t in stale]
                logger.critical(
                    "State recovery: %d DB OPEN row(s) with NO broker position — moved to "
                    "CLOSING for fill reconciliation (not re-sold): %s", len(stale), names,
                )
                await publish("system_alerts", {
                    "type": "critical",
                    "message": (
                        f"{len(stale)} DB position(s) are flat at the broker: {names}. "
                        f"Marked CLOSING for reconciliation from broker fills — not re-sold."
                    ),
                    "timestamp": datetime.now(IST).isoformat(),
                })
                await send_telegram(
                    f"⚠️ State recovery: {names} OPEN in DB but flat at the broker. "
                    f"Booking from broker fills — verify P&L."
                )

            orphans = {sym: pos_by_sym[sym] for sym in longs if sym not in db_open}

            if not orphans:
                logger.info(
                    "State recovery complete: %d broker long(s) matched, %d short(s), "
                    "%d DB row(s) flat at broker",
                    len(longs), len(shorts), len(stale),
                )
                return

            logger.warning(
                "State recovery: %d orphaned Groww position(s) with no DB record — "
                "creating synthetic OPEN entries: %s",
                len(orphans), list(orphans.keys()),
            )

            unprotected: list = []
            async with get_db_context() as session:
                for sym, pos in orphans.items():
                    avg_price = float(pos.get("average_price") or 0)
                    if avg_price <= 0:
                        logger.warning(
                            "State recovery: %s has avg_price=0 — skipping synthetic record", sym
                        )
                        continue
                    qty = int(longs[sym])  # strictly positive: shorts never reach here
                    tick = get_tick_size(sym)
                    sl = floor_to_tick(avg_price * (1 - settings.stop_loss_pct), tick)
                    tgt = ceil_to_tick(avg_price * (1 + settings.min_target_pct), tick)
                    exchange = pos.get("exchange", "NSE") or "NSE"
                    sl_order_id = await self._recover_stop(groww, sym, exchange, qty, sl)
                    if not sl_order_id or order_lifecycle.is_unresolved_stop(sl_order_id):
                        unprotected.append(sym)
                    synthetic = Trade(
                        entry_order_id=f"RECOVERED-{sym}-{today.isoformat()}",
                        stock=sym,
                        exchange=exchange,
                        direction="BUY",
                        product_type="MIS",
                        quantity=qty,
                        original_quantity=qty,
                        entry_price=avg_price,
                        stop_loss_price=sl,
                        target_price=tgt,
                        status="OPEN",
                        trade_date=today,
                        entry_time=now_time,
                        decision_rationale="RECOVERED — no DB record found on startup",
                        sl_order_id=sl_order_id,
                    )
                    session.add(synthetic)
                    logger.warning(
                        "Synthetic Trade created: %s qty=%d avg=₹%.2f SL=₹%.2f TGT=₹%.2f stop=%s",
                        sym, qty, avg_price, sl, tgt, sl_order_id,
                    )

            if unprotected:
                await set_halt(f"recovered position(s) without a confirmed broker stop: {unprotected}")
                await send_telegram(
                    f"\U0001f6a8 State recovery adopted {unprotected} but NO broker stop could be "
                    f"confirmed. Trading HALTED — place / verify the stop in Groww now."
                )

            await publish("system_alerts", {
                "type": "warning",
                "message": (
                    f"State recovery created {len(orphans)} synthetic trade record(s) for "
                    f"orphaned Groww position(s): {list(orphans.keys())}. "
                    f"SL/target estimated from config. Manual review recommended."
                ),
                "timestamp": datetime.now(IST).isoformat(),
            })

        except Exception as exc:
            logger.error("State recovery DB reconciliation failed: %s", exc)
            await set_halt(f"state recovery DB reconciliation failed: {exc}")

    async def _recover_stop(
        self, groww, symbol: str, exchange: str, qty: int, stop_price: float,
    ) -> Optional[str]:
        """Broker stop for an adopted orphan position (see
        ``order_lifecycle.adopt_or_place_stop``)."""
        return await order_lifecycle.adopt_or_place_stop(groww, symbol, exchange, qty, stop_price)

    async def _hydrate_paper_broker(self) -> None:
        """Re-create today's OPEN / CLOSING paper positions (and their stops)
        in the PaperBroker after a restart, so exits and the reconciler see
        the same book as the DB."""
        broker = get_broker(self._settings)
        adopt = getattr(broker, "adopt_position", None)
        if adopt is None:
            return
        today = datetime.now(IST).date()
        try:
            async with get_db_context() as session:
                result = await session.execute(
                    select(Trade).where(
                        Trade.status.in_(["OPEN", "CLOSING"]),
                        Trade.trade_date == today,
                    )
                )
                rows = list(result.scalars().all())
            for t in rows:
                sl_id = adopt(
                    t.stock, int(t.quantity), float(t.entry_price), t.exchange or "NSE",
                    stop_order_id=t.sl_order_id, stop_price=t.stop_loss_price,
                )
                if sl_id != t.sl_order_id:
                    await self._positions().set_sl_order_id(t, sl_id)
            if rows:
                logger.info("Paper broker hydrated with %d open position(s) from the DB", len(rows))
        except Exception as exc:
            logger.error("Paper broker hydration failed: %s", exc)
            await set_halt(f"paper broker hydration failed: {exc}")

    async def _on_market_brief(self, data: str) -> None:
        """Handler for Redis messages on the 'market_brief' channel."""
        try:
            brief_data = json.loads(data)
            brief = MarketBriefLLMOutput.model_validate(brief_data)
            self._decision_engine.set_market_brief(brief)
            self._scanner.set_market_bias(brief.market_bias.value)
        except Exception as exc:
            logger.error("Failed to parse market brief from Redis: %s", exc)

    async def _process_decisions(self) -> None:
        """
        Consume signals from the queue, run them through the Decision Engine,
        and place orders for approved trades.
        """
        while self._running:
            try:
                signal: ScannerSignal = await asyncio.wait_for(
                    self._signal_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error("Signal queue error: %s", exc)
                continue

            now_ist = datetime.now(IST)

            # Market hours guard: belt-and-suspenders (Scanner already filters at 3:15 PM)
            if (now_ist.hour, now_ist.minute) >= (15, 15):
                logger.debug("Past 3:15 PM IST — discarding late signal for %s", signal.stock)
                continue

            # Track last signal for dashboard visibility
            await set_value("agent:trading:last_signal_stock", signal.stock)
            await set_value("agent:trading:last_signal_time", now_ist.isoformat())

            # Check halt flag — fail closed: unreadable state counts as halted (C8)
            if await is_halted_strict():
                logger.info("Trading halted — skipping signal for %s", signal.stock)
                await publish("system_alerts", {
                    "type": "warning",
                    "message": f"Signal ignored ({signal.stock}): trading is halted",
                    "timestamp": now_ist.isoformat(),
                })
                continue

            decision = await self._decision_engine.process_signal(signal)
            if decision is None:
                continue

            # Shielded: once the order is in flight a cancellation (15:30 stop)
            # must not cut it off between the order and its DB write (C9).
            # One bad signal must never end the decision loop.
            try:
                await asyncio.shield(self._execute_trade(signal, decision))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Trade execution failed for %s: %s", signal.stock, exc)

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

    async def _execute_trade(self, signal: ScannerSignal, decision: DecisionOutput) -> None:
        """Place the entry and record the position (paper and live share this path).

        Outbox (audit C9): a PENDING row carrying the order_reference_id is
        written BEFORE the order is sent; a DB failure there sends nothing.
        The fill is confirmed from the broker (``filled_quantity`` /
        ``average_price``), SL / target are recomputed from the fill, the
        broker stop is placed, and only then does the row become OPEN.  A
        nothing-filled entry becomes REJECTED; an unknown one stays PENDING
        for the reconciler and halts new entries.
        """
        settings = self._settings
        now_ist = datetime.now(IST)
        broker = get_broker(settings)

        # Product is ALWAYS MIS (audit R11) — re-asserted here even though
        # _validate_decision already forces it, so no code path can ever send
        # a CNC/DELIVERY buy.
        requested_product = getattr(getattr(decision, "product_type", None), "value", "MIS")
        if requested_product != "MIS":
            logger.error(
                "Decision for %s carried product_type=%s — forcing MIS",
                signal.stock, requested_product,
            )
        product = "MIS"

        # A3: skip entries at / within 0.5% of the upper circuit (NSE would
        # reject them silently).  A failed quote does not block the entry.
        try:
            stock_quote = (await broker.get_quote([f"NSE:{signal.stock}"])).get(f"NSE:{signal.stock}") or {}
            upper_circuit = float(stock_quote.get("upper_circuit_limit") or 0)
            if upper_circuit > 0 and signal.ltp >= upper_circuit * 0.995:
                logger.warning(
                    "Circuit limit: %s LTP ₹%.2f >= upper circuit ₹%.2f (99.5%%) — order skipped",
                    signal.stock, signal.ltp, upper_circuit,
                )
                await publish("system_alerts", {
                    "type": "warning",
                    "message": (
                        f"Order skipped: {signal.stock} at/near upper circuit ₹{upper_circuit:.2f} "
                        f"— circuit limit buy rejected"
                    ),
                    "timestamp": now_ist.isoformat(),
                })
                return
        except Exception as circuit_exc:
            logger.warning(
                "Circuit limit check failed for %s: %s — proceeding with order",
                signal.stock, circuit_exc,
            )

        positions = self._positions()
        ref_id = new_order_reference_id()
        try:
            trade = await positions.create_pending(
                stock=signal.stock,
                exchange=signal.exchange,
                quantity=decision.adjusted_qty,
                entry_price=signal.ltp,
                stop_loss_price=decision.stop_loss_price,
                target_price=decision.target_price,
                order_reference_id=ref_id,
                rationale=decision.rationale,
                now_ist=now_ist,
            )
        except Exception as exc:
            logger.error("Could not record PENDING entry for %s: %s — order NOT sent", signal.stock, exc)
            return

        with order_lifecycle.trade_in_flight(trade.id):
            entry = await self._send_entry(signal, decision, trade, ref_id, broker, positions, now_ist)
        if entry is None:
            return
        fill_price, filled_qty, decision = entry

        # Store ATR in Redis for the ROI decay floor in the risk manager.
        # TTL = 86400 s (24 h) — always expires before the next session open.
        if signal.atr and signal.atr > 0:
            await set_value(f"trade_atr:{signal.stock}", str(round(signal.atr, 4)), ttl=86400)

        # Increment daily trade counter.
        # Both EXECUTE and REDUCE decisions count toward max_trades_per_day.
        # Rationale for counting REDUCE:
        #  1. Consistency — _restore_trade_count queries the DB counting ALL trades,
        #     so the counter semantics must match or the value drifts after a restart.
        #  2. Safety — excluding REDUCE created a loophole where a day could exceed
        #     the intended daily trade limit if all signals came back as REDUCE.
        #  3. Accuracy — REDUCE opens a real position and consumes daily capacity.
        await increment(TRADE_COUNT_KEY)

        # Publish trade event to Redis
        await publish("trade_events", {
            "type": "TRADE_OPENED",
            "stock": signal.stock,
            "price": fill_price,
            "stop_loss": decision.stop_loss_price,
            "target": decision.target_price,
            "qty": filled_qty,
            "product_type": product,
            "timestamp": now_ist.isoformat(),
        })

        # Telegram alert
        await send_trade_entry_alert(
            stock=signal.stock,
            price=fill_price,
            sl=decision.stop_loss_price,
            target=decision.target_price,
            qty=filled_qty,
            product_type=product,
            rationale=decision.rationale or "",
            paper=settings.paper_trading,
        )

        logger.info(
            "✅ Trade executed: BUY %s @ ₹%.2f | SL: ₹%.2f | TGT: ₹%.2f | Qty: %d | %s",
            signal.stock, fill_price, decision.stop_loss_price,
            decision.target_price, filled_qty, product,
        )

    async def _send_entry(self, signal, decision, trade, ref_id, broker, positions, now_ist):
        """Send the entry BUY for the PENDING *trade* and resolve it.

        Returns ``(fill_price, filled_qty, decision)`` once the row is OPEN, or
        None (row REJECTED when nothing filled; left PENDING and trading HALTED
        when the outcome is unknown).
        """
        settings = self._settings
        try:
            order_id = await broker.place_order(
                tradingsymbol=signal.stock,
                exchange=signal.exchange or "NSE",
                transaction_type="BUY",
                quantity=decision.adjusted_qty,
                product="MIS",
                order_reference_id=ref_id,
            )
        except OrderStateUnknownError as unknown_exc:
            # The BUY may be live with no stop.  Never resubmit (audit R5): the
            # row stays PENDING for the reconciler; halt and escalate.
            logger.critical(
                "BUY for %s in UNKNOWN state (ref=%s): %s — halting",
                signal.stock, unknown_exc.order_reference_id, unknown_exc,
            )
            await set_halt(f"BUY {signal.stock} state unknown (ref {unknown_exc.order_reference_id})")
            await send_telegram(
                f"\U0001f6a8 BUY {signal.stock} state UNKNOWN "
                f"(ref {unknown_exc.order_reference_id}).\n"
                f"It may be filled with NO stop-loss. Trading halted — "
                f"check the broker order book now."
            )
            return None
        except Exception as exc:
            logger.error("Order placement failed for %s: %s", signal.stock, exc)
            await positions.reject_entry(trade, f"order placement failed: {exc}")
            return None

        # ── Entry fill confirmation (audit R3 / C5) ──────────────────────────
        # Wait for a TERMINAL broker state with a timeout (the unfilled
        # remainder is cancelled).  Position and stop are sized from the
        # broker's filled_quantity and priced from average_price.
        if not order_id:
            logger.critical(
                "BUY for %s returned no broker order id — fill cannot be tracked; halting",
                signal.stock,
            )
            await set_halt(f"BUY {signal.stock} returned no broker order id")
            await send_telegram(
                f"\U0001f6a8 BUY {signal.stock}: broker returned no order id (ref {ref_id}).\n"
                f"Fill state UNKNOWN — trading halted, check the broker order book."
            )
            return None

        fill = await order_lifecycle.confirm_fill(
            broker, str(order_id), order_lifecycle.ENTRY_FILL_TIMEOUT_S,
        )
        broker_filled = int(fill.filled_qty) if fill is not None else 0
        if fill is None or (not fill.terminal and broker_filled <= 0):
            logger.critical(
                "BUY %s for %s: state UNKNOWN after timeout (status=%s) — halting",
                order_id, signal.stock, fill.status if fill else "UNREADABLE",
            )
            await set_halt(f"BUY {signal.stock} (order {order_id}) not confirmed")
            await send_telegram(
                f"\U0001f6a8 BUY {signal.stock} (order {order_id}) not confirmed.\n"
                f"It may still fill with NO stop-loss. Trading halted — "
                f"check the broker order book now."
            )
            return None

        if broker_filled <= 0:
            await positions.reject_entry(trade, f"BUY {order_id} {fill.status}, nothing filled")
            await publish("system_alerts", {
                "type": "warning",
                "message": f"BUY {signal.stock} {fill.status}: nothing filled — no position opened",
                "timestamp": now_ist.isoformat(),
            })
            await send_telegram(
                f"⚠️ BUY {signal.stock} {fill.status} (order {order_id}) — "
                f"nothing filled, no position opened."
            )
            return None

        filled_qty = min(broker_filled, int(decision.adjusted_qty))
        if not fill.terminal:
            # Remainder cancel unconfirmed: a late fill leaves a small unmanaged
            # LONG (exits never sell more than held), never a short.
            await set_halt(f"BUY {signal.stock} (order {order_id}) remainder cancel not confirmed")
            await send_telegram(
                f"\U0001f6a8 BUY {signal.stock} (order {order_id}): {filled_qty} filled, "
                f"remainder cancel NOT confirmed. Position recorded at {filled_qty}; "
                f"trading halted — check the broker order book."
            )
        elif filled_qty < decision.adjusted_qty:
            logger.warning(
                "Partial entry fill for %s: %d/%d — sizing position and stop to the fill",
                signal.stock, filled_qty, decision.adjusted_qty,
            )

        fill_price = signal.ltp
        if fill.average_price:
            fill_price = float(fill.average_price)
            if fill_price != signal.ltp:
                logger.info(
                    "Slippage for %s: signal ₹%.2f → fill ₹%.2f (%.3f%%)",
                    signal.stock, signal.ltp, fill_price,
                    (fill_price - signal.ltp) / signal.ltp * 100,
                )
        else:
            logger.warning("No average fill price for %s order %s — using signal LTP", signal.stock, order_id)

        if filled_qty != decision.adjusted_qty:
            decision = decision.model_copy(update={"adjusted_qty": filled_qty})

        # SL / target from the actual fill: SL floored, target ceiled (R10).
        if fill_price != signal.ltp:
            new_sl, new_tgt = exit_policy.initial_levels(
                fill_price, signal.atr, settings, get_tick_size(signal.stock),
            )
            decision = decision.model_copy(update={"stop_loss_price": new_sl, "target_price": new_tgt})

        # Broker stop for the FILLED quantity, full raw order id kept (R1 / C1).
        sl_order_id = await order_lifecycle.place_stop(
            broker, signal.stock, signal.exchange or "NSE", filled_qty, decision.stop_loss_price,
        )
        if sl_order_id and not order_lifecycle.is_unresolved_stop(sl_order_id):
            logger.info("Broker SL placed for %s: order_id=%s qty=%d", signal.stock, sl_order_id, filled_qty)
        else:
            # None = failed; ``ref:<id>`` = ambiguous (persisted so every exit
            # resolves it).  The RiskManager still enforces the DB stop.
            logger.error(
                "Broker SL placement failed for %s — halting new trades, manual stop required",
                signal.stock,
            )
            await set_halt(f"broker stop-loss placement failed for {signal.stock}")
            await publish("system_alerts", {
                "type": "critical",
                "message": (
                    f"Broker stop-loss FAILED for {signal.stock}. "
                    f"Position is LIVE with no server-side stop. "
                    f"Trading HALTED — manual intervention required."
                ),
                "timestamp": now_ist.isoformat(),
            })
            await send_telegram(
                f"\U0001f6a8 STOP-LOSS ORDER FAILED for {signal.stock}\n"
                f"Position is LIVE with NO server-side stop-loss.\n"
                f"Trading halted — manual intervention required."
            )

        partial_target_price = exit_policy.partial_target(
            fill_price, decision.stop_loss_price, decision.target_price, filled_qty, settings,
        )

        try:
            opened = await positions.confirm_entry(
                trade,
                order_id=str(order_id),
                quantity=filled_qty,
                entry_price=fill_price,
                stop_loss_price=decision.stop_loss_price,
                target_price=decision.target_price,
                partial_target_price=partial_target_price,
                sl_order_id=sl_order_id,
            )
        except Exception as exc:
            logger.critical("Could not mark entry %s OPEN: %s", signal.stock, exc)
            opened = False
        if not opened:
            # The position is live but its row is still PENDING: the reconciler
            # adopts it (and its stop) from the broker.  No new entries meanwhile.
            await set_halt(f"filled BUY {signal.stock} (order {order_id}) could not be recorded OPEN")
            await send_telegram(
                f"\U0001f6a8 BUY {signal.stock} filled ({filled_qty}) but the DB row could not be "
                f"marked OPEN. Trading halted — the reconciler will adopt it; verify in the broker."
            )
            return None
        return fill_price, filled_qty, decision

    async def start(self) -> None:
        """Start all sub-modules of the Trading Agent."""
        logger.info("═══ Trading Agent starting ═══")
        await set_value("agent:trading:status", "ACTIVE")

        # State recovery — MANDATORY first step
        await self.state_recovery()

        # Subscribe to market brief channel
        await subscribe("market_brief", self._on_market_brief)

        # Load latest market brief from Redis if available
        latest_brief = await get_value("latest_market_brief")
        if latest_brief:
            await self._on_market_brief(latest_brief)

        # Clear stale LTPs from any previous session so paper-trading RiskManager
        # polls cannot evaluate SL/target against yesterday's prices on day restart.
        ltp_store_module.clear()
        logger.info("LTP store cleared for new trading session")

        # NOTE: daily_trade_count is intentionally NOT reset here.
        # TradingAgentManager.start_session() restores the correct value
        # from the database before calling this method, so a mid-day restart
        # preserves the accurate count rather than wiping it to zero.

        self._running = True

        # Start Risk Manager on its own thread
        # get_running_loop() is the correct call inside an async function — it
        # always returns the currently-executing event loop and raises RuntimeError
        # if called outside one (which would be a programming error here).
        loop = asyncio.get_running_loop()
        self._risk_manager.start(loop)

        # Periodic broker ⇄ DB reconciliation (audit C7).  Not part of the
        # supervised set: it never raises, and must not stop the agent.
        reconciler_task = asyncio.create_task(
            self._reconciler.run(lambda: self._running), name="reconciler",
        )

        # Start Scanner (WebSocket) and Decision processing concurrently
        # with supervision: if either crashes, log + alert and stop cleanly.
        scanner_task = asyncio.create_task(self._scanner.start(), name="scanner")
        decision_task = asyncio.create_task(self._process_decisions(), name="decision_processor")

        try:
            done, pending = await asyncio.wait(
                [scanner_task, decision_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )

            # If one finished with an exception, cancel the other and surface the error
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logger.critical(
                        "Component '%s' crashed: %s — stopping Trading Agent",
                        task.get_name(), exc,
                    )
                    await publish("system_alerts", {
                        "type": "critical",
                        "message": f"Component {task.get_name()} crashed: {exc}",
                        "timestamp": datetime.now(IST).isoformat(),
                    })
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            reconciler_task.cancel()
            # Guarantee the Risk Manager thread is always stopped when start() exits,
            # whether due to a normal 15:30 stop(), scanner crash, or cancellation.
            # RiskManager.stop() is idempotent — safe to call even if already stopped.
            self._risk_manager.stop()
            logger.info("Risk Manager stopped in start() finally block")

    async def stop(self) -> None:
        """Stop all sub-modules."""
        self._running = False
        self._scanner.stop()
        self._decision_engine.stop()
        self._risk_manager.stop()
        await set_value("agent:trading:status", "INACTIVE")
        logger.info("═══ Trading Agent stopped ═══")
