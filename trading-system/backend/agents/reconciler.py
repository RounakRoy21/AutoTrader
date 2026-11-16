"""
Reconciler — periodic two-way broker ⇄ DB diff (audit Part B C7 / C9).

Runs every ``reconcile_interval_s`` (30-60 s) during market hours, alongside
startup state recovery.  The broker is the source of truth; every DB change
goes through ``PositionService`` CAS transitions.

Per cycle:
  * Stranded PENDING entries (outbox rows older than ``PENDING_GRACE_S`` whose
    entry is not in flight) are resolved by their order_reference_id: never
    placed / nothing filled → REJECTED; filled → OPEN with its stop adopted
    (or placed).  An unresolvable row is alerted and retried.
  * Net MIS shorts → HALT + alert (a long-only system never shorts on purpose).
  * Mismatches must be seen on two consecutive cycles before acting (the
    broker's position view can lag a fill):
      - DB OPEN, broker flat → row moved to CLOSING; the RiskManager's CLOSING
        reconciliation books it from the broker fill (never re-sold).
      - broker long with no DB row, or differing quantities → alert only (a
        manual trade in the broker app must not be touched).
      - a working SELL stop for a symbol that is flat at the broker with no
        DB row → cancelled (it would open a short when triggered).
  * Symbols with an in-flight entry / exit are skipped for the cycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, time as dt_time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pytz
from sqlalchemy import select

from core.database import get_db_context
from core.redis_client import publish
from core.risk_state import set_halt
from integrations.broker import get_broker
from integrations.telegram_client import send_telegram
from models.trade import Trade

from . import order_lifecycle
from .position_service import PositionDeps, PositionService

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 35)
PENDING_GRACE_S = 30.0


def _no_costs(*_a, **_kw) -> float:
    return 0.0


async def _no_outcome(*_a, **_kw) -> None:
    return None


class Reconciler:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._day: Optional[date] = None
        self._mismatch_seen: Dict[str, Tuple[int, int]] = {}
        self._orphan_stops_seen: Set[str] = set()
        self._alerted: Set[str] = set()

    def _positions(self) -> PositionService:
        # The reconciler never books P&L (CLOSING rows are booked by the
        # RiskManager), so costs / outcome hooks are inert.
        return PositionService(self._settings, PositionDeps(
            db_context=get_db_context,
            broker=lambda: get_broker(self._settings),
            publish=publish,
            notify=send_telegram,
            costs=_no_costs,
            record_outcome=_no_outcome,
        ))

    async def run(self, running: Callable[[], bool]) -> None:
        """Loop until *running()* is False.  Never raises."""
        interval = float(getattr(self._settings, "reconcile_interval_s", 45.0) or 45.0)
        while running():
            now_ist = datetime.now(IST)
            if MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE:
                try:
                    await self.reconcile_once(now_ist)
                except Exception as exc:
                    logger.exception("Reconciliation cycle failed: %s", exc)
            await asyncio.sleep(interval)

    # ── One cycle ────────────────────────────────────────────────────────────

    async def reconcile_once(self, now_ist: Optional[datetime] = None) -> Dict[str, List[str]]:
        now_ist = now_ist or datetime.now(IST)
        if self._day != now_ist.date():
            self._day = now_ist.date()
            self._mismatch_seen.clear()
            self._orphan_stops_seen.clear()
            self._alerted.clear()

        report: Dict[str, List[str]] = defaultdict(list)
        broker = get_broker(self._settings)

        net = await self._broker_net(broker)
        if net is None:
            await self._alert_once(
                "positions", "Reconciler: broker positions unreadable — cycle skipped", critical=False,
            )
            return report

        async with get_db_context() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.status.in_(["PENDING", "OPEN", "CLOSING"]),
                    Trade.trade_date == now_ist.date(),
                )
            )
            rows = list(result.scalars().all())

        busy: Set[str] = {t.stock for t in rows if order_lifecycle.is_trade_in_flight(t.id)}
        for t in rows:
            if t.status != "PENDING":
                continue
            # The positions snapshot may predate the resolution: no symbol
            # diff for it this cycle.
            busy.add(t.stock)
            if order_lifecycle.is_trade_in_flight(t.id) or self._age_s(t, now_ist) < PENDING_GRACE_S:
                continue
            await self._resolve_pending(t, broker, report)

        db_qty: Dict[str, int] = defaultdict(int)
        db_rows: Dict[str, List[Trade]] = defaultdict(list)
        for t in rows:
            if t.status in ("OPEN", "CLOSING"):
                db_qty[t.stock] += int(t.quantity or 0)
                db_rows[t.stock].append(t)

        for sym in sorted(set(net) | set(db_qty)):
            if sym in busy:
                self._mismatch_seen.pop(sym, None)
                continue
            await self._diff_symbol(sym, net.get(sym, 0), db_qty.get(sym, 0), db_rows[sym], report)

        await self._check_stops(broker, net, db_qty, db_rows, busy, report)
        return report

    async def _broker_net(self, broker: Any) -> Optional[Dict[str, int]]:
        """Signed net MIS quantity per symbol; None when unreadable."""
        try:
            positions = await broker.get_positions()
        except Exception as exc:
            logger.warning("Reconciler: positions fetch failed: %s", exc)
            return None
        net: Dict[str, int] = defaultdict(int)
        for p in (positions or {}).get("net", []) or []:
            sym = str(p.get("tradingsymbol") or "")
            if not sym or str(p.get("product") or "MIS").upper() != "MIS":
                continue
            try:
                net[sym] += int(p.get("quantity") or 0)
            except (TypeError, ValueError):
                return None
        return {s: q for s, q in net.items() if q != 0}

    @staticmethod
    def _age_s(trade: Trade, now_ist: datetime) -> float:
        try:
            created = IST.localize(datetime.combine(trade.trade_date, trade.entry_time))
        except Exception:
            return float("inf")
        return (now_ist - created).total_seconds()

    # ── PENDING entries ──────────────────────────────────────────────────────

    async def _resolve_pending(self, t: Trade, broker: Any, report: Dict[str, List[str]]) -> None:
        positions = self._positions()
        label = f"{t.stock}#{t.id}"
        ref = getattr(t, "entry_order_ref", None)
        if not ref:
            await self._alert_once(f"pending-noref:{t.id}", f"PENDING entry {label} has no order reference — resolve manually")
            return
        try:
            order_id = await broker.find_order_by_reference(ref)
        except Exception as exc:
            await self._alert_once(
                f"pending-lookup:{t.id}",
                f"PENDING entry {label} (ref {ref}) could not be resolved at the broker: {exc} — retrying",
            )
            return
        if order_id is None:
            if await positions.reject_entry(t, f"entry order ref {ref} never reached the broker"):
                report["pending_rejected"].append(label)
            return

        fill = await order_lifecycle.read_order(broker, str(order_id))
        if fill is None:
            return
        if not fill.terminal:
            await self._alert_once(
                f"pending-working:{t.id}",
                f"PENDING entry {label}: order {order_id} still {fill.status} at the broker — waiting",
                critical=False,
            )
            return
        if fill.filled_qty <= 0:
            if await positions.reject_entry(t, f"entry order {order_id} {fill.status}, nothing filled"):
                report["pending_rejected"].append(label)
            return

        qty = int(fill.filled_qty)
        price = float(fill.average_price or t.entry_price)
        sl_id = await order_lifecycle.adopt_or_place_stop(
            broker, t.stock, t.exchange or "NSE", qty, t.stop_loss_price,
        )
        if not await positions.confirm_entry(
            t,
            order_id=str(order_id),
            quantity=qty,
            entry_price=price,
            stop_loss_price=t.stop_loss_price,
            target_price=t.target_price,
            partial_target_price=None,
            sl_order_id=sl_id,
        ):
            return
        report["pending_opened"].append(label)
        protected = bool(sl_id) and not order_lifecycle.is_unresolved_stop(sl_id)
        if not protected:
            await set_halt(f"reconciled entry {label} has no confirmed broker stop")
        await self._alert(
            f"Reconciler: stranded entry {label} was FILLED ({qty} @ ₹{price:.2f}) — now OPEN"
            + (f" with broker stop {sl_id}" if protected else " with NO confirmed broker stop; trading HALTED"),
            critical=not protected,
        )

    # ── Positions ────────────────────────────────────────────────────────────

    async def _diff_symbol(
        self, sym: str, broker_qty: int, db_qty: int, rows: List[Trade], report: Dict[str, List[str]],
    ) -> None:
        if broker_qty < 0:
            self._mismatch_seen.pop(sym, None)
            if f"short:{sym}" not in self._alerted:
                await set_halt(f"net short {broker_qty} in {sym} at the broker")
                report["net_shorts"].append(sym)
            await self._alert_once(
                f"short:{sym}",
                f"Broker holds a NET SHORT of {broker_qty} {sym} in a long-only system. "
                f"Trading HALTED — flatten it manually.",
            )
            return
        if broker_qty == db_qty:
            self._mismatch_seen.pop(sym, None)
            return
        signature = (db_qty, broker_qty)
        if self._mismatch_seen.get(sym) != signature:
            self._mismatch_seen[sym] = signature   # first sighting: confirm next cycle
            return

        if broker_qty == 0:
            positions = self._positions()
            moved = [t for t in rows if t.status == "OPEN" and await positions.mark_closing(t)]
            report["flat_to_closing"].extend(f"{sym}#{t.id}" for t in moved)
            if moved:
                await self._alert(
                    f"Reconciler: {sym} is flat at the broker but OPEN in the DB (broker stop fired?) — "
                    f"moved to CLOSING; it will be booked from the broker fill",
                    critical=False,
                )
            self._mismatch_seen.pop(sym, None)
        elif db_qty == 0:
            report["orphan_longs"].append(sym)
            await self._alert_once(
                f"orphan:{sym}:{broker_qty}",
                f"Broker holds {broker_qty} {sym} (MIS) with no AutoTrader position — not managed, "
                f"no stop. Check the broker.",
            )
        else:
            report["qty_mismatch"].append(sym)
            await self._alert_once(
                f"qty:{sym}:{db_qty}:{broker_qty}",
                f"{sym}: DB quantity {db_qty} but the broker holds {broker_qty} — reconcile manually",
            )

    # ── Open orders ──────────────────────────────────────────────────────────

    async def _check_stops(
        self,
        broker: Any,
        net: Dict[str, int],
        db_qty: Dict[str, int],
        db_rows: Dict[str, List[Trade]],
        busy: Set[str],
        report: Dict[str, List[str]],
    ) -> None:
        stops = await order_lifecycle.working_sell_stops_by_symbol(broker)
        if stops is None:
            return
        seen_now: Set[str] = set()
        for sym, order_ids in stops.items():
            if sym in busy:
                continue
            if db_qty.get(sym, 0) == 0 and net.get(sym, 0) <= 0:
                for oid in order_ids:
                    seen_now.add(oid)
                    if oid not in self._orphan_stops_seen:
                        continue
                    fill = await order_lifecycle.cancel_and_confirm(broker, oid)
                    if fill is not None and fill.terminal and fill.status == "CANCELLED":
                        report["orphan_stops_cancelled"].append(oid)
                        await self._alert(
                            f"Reconciler: cancelled orphan SELL stop {oid} for {sym} (no position) — "
                            f"it would have opened a short",
                            critical=False,
                        )
                    else:
                        await self._alert_once(
                            f"orphan-stop:{oid}",
                            f"Orphan SELL stop {oid} for {sym} (no position) could not be cancelled — "
                            f"cancel it manually before it opens a short",
                        )
                continue
            known = {t.sl_order_id for t in db_rows.get(sym, []) if t.sl_order_id}
            extras = [oid for oid in order_ids if oid not in known]
            if extras and known:
                await self._alert_once(
                    f"extra-stops:{sym}:{','.join(extras)}",
                    f"{sym}: extra working SELL stop(s) {extras} beyond the position's own stop — "
                    f"together they could oversell; check the broker",
                )
        self._orphan_stops_seen = seen_now

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def _alert(self, msg: str, critical: bool = True) -> None:
        (logger.critical if critical else logger.warning)(msg)
        try:
            await publish("system_alerts", {
                "type": "critical" if critical else "warning",
                "message": msg,
                "timestamp": datetime.now(IST).isoformat(),
            })
            await send_telegram(f"{chr(0x1F6A8) if critical else chr(0x26A0)} {msg}")
        except Exception as exc:
            logger.warning("Reconciler alert failed: %s", exc)

    async def _alert_once(self, key: str, msg: str, critical: bool = True) -> None:
        if key in self._alerted:
            return
        self._alerted.add(key)
        await self._alert(msg, critical=critical)
