"""
Trading Agent — Orchestrates Scanner, Decision Engine, and Risk Manager.

Activates at 9:15 AM IST, deactivates at 3:30 PM IST.
On startup, runs state recovery to reconstruct open positions from Kite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dt_time
from typing import Optional
from uuid import uuid4

import pytz
from sqlalchemy import select

from core.config import get_settings
from core.database import get_db_context
from core.redis_client import get_value, increment, publish, set_value, subscribe
from core.redis_keys import HALT_KEY, TRADING_STATUS_KEY, DAILY_TRADE_COUNT_KEY
from integrations import ltp_store as ltp_store_module
from integrations.kite_client import get_kite_client
from integrations.telegram_client import send_telegram, send_trade_entry_alert
from models.trade import Trade
from schemas.decision import DecisionOutput
from schemas.market_brief import MarketBriefLLMOutput
from schemas.trade import ScannerSignal

from .decision_engine import DecisionEngine
from .risk_manager import RiskManager
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
        self._running = False

    async def state_recovery(self) -> None:
        """
        MANDATORY first step: log open positions recovered from Kite at session start.
        Open position counting is now handled by live DB queries in the Decision Engine,
        so no counter synchronisation is required here.
        """
        logger.info("Running state recovery — syncing open positions from Kite…")
        try:
            kite = get_kite_client()
            positions = await kite.get_positions()
            net_positions = positions.get("net", [])
            open_count = sum(1 for p in net_positions if p.get("quantity", 0) != 0)
            for pos in net_positions:
                if pos.get("quantity", 0) != 0:
                    logger.info(
                        "Recovered position: %s qty=%d avg_price=%.2f",
                        pos.get("tradingsymbol"), pos.get("quantity"),
                        pos.get("average_price", 0),
                    )
            logger.info("State recovery complete: %d open positions", open_count)
        except Exception as exc:
            logger.error("State recovery failed: %s — proceeding with empty state", exc)

    async def _on_market_brief(self, data: str) -> None:
        """Handler for Redis messages on the 'market_brief' channel."""
        try:
            brief_data = json.loads(data)
            brief = MarketBriefLLMOutput.model_validate(brief_data)
            self._decision_engine.set_market_brief(brief)
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

            # Check halt flag
            halt = await get_value(HALT_KEY)
            if halt == "TRUE":
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

            await self._execute_trade(signal, decision)

    async def _execute_trade(self, signal: ScannerSignal, decision: DecisionOutput) -> None:
        """Place the order and record the trade.

        In live mode, verifies the actual fill price from Kite and
        recalculates SL/target from it so that slippage doesn't skew the
        risk-reward profile.
        """
        settings = self._settings
        now_ist = datetime.now(IST)
        kite = get_kite_client()
        fill_price = signal.ltp  # default; overridden by actual fill in live mode

        try:
            if settings.paper_trading:
                order_id = f"PAPER-{now_ist.strftime('%H%M%S')}-{signal.stock}-{uuid4().hex[:6]}"
                logger.info("📝 Paper trade: %s", order_id)
            else:
                order_id = await kite.place_order(
                    tradingsymbol=signal.stock,
                    transaction_type="BUY",
                    quantity=decision.adjusted_qty,
                    product=decision.product_type.value,
                )

                # Verify actual fill price.  NSE MARKET orders usually complete
                # within 1–2 s, but during the opening 30 min (9:15–9:45) and on
                # high-volatility days exchange acknowledgment can take up to 5 s.
                # A single 0.5 s sleep missed fills frequently in the most active
                # trading window, leaving fill_price = signal.ltp and losing
                # accurate slippage and SL/target recalculation.
                # Retry up to 3 times (≤ 1.5 s total) before falling back.
                try:
                    for _attempt in range(3):
                        await asyncio.sleep(0.5)
                        history = await kite.get_order_history(order_id)
                        for entry in reversed(history):
                            if entry.get("status") == "COMPLETE" and entry.get("average_price"):
                                fill_price = entry["average_price"]
                                if fill_price != signal.ltp:
                                    slippage_pct = ((fill_price - signal.ltp) / signal.ltp) * 100
                                    logger.info(
                                        "Slippage for %s: signal ₹%.2f → fill ₹%.2f (%.3f%%)",
                                        signal.stock, signal.ltp, fill_price, slippage_pct,
                                    )
                                break
                        else:
                            continue  # COMPLETE entry not found yet — retry
                        break  # fill confirmed — exit retry loop
                except Exception as fill_exc:
                    logger.warning(
                        "Could not verify fill price for %s: %s — using signal LTP",
                        signal.stock, fill_exc,
                    )

                # Recalculate SL and target based on actual fill price
                if fill_price != signal.ltp:
                    if signal.atr and signal.atr > 0:
                        new_sl = round(fill_price - signal.atr * settings.atr_sl_multiplier, 2)
                        new_tgt = round(fill_price + signal.atr * settings.atr_target_multiplier, 2)
                    else:
                        new_sl = round(fill_price * (1 - settings.stop_loss_pct), 2)
                        new_tgt = round(fill_price * (1 + settings.min_target_pct), 2)
                    decision = decision.model_copy(update={
                        "stop_loss_price": new_sl,
                        "target_price": new_tgt,
                    })

                # Place GTT stop-loss (server-side at Zerodha)
                gtt_trigger_id = None
                try:
                    gtt_resp = await kite.place_gtt(
                        tradingsymbol=signal.stock,
                        exchange="NSE",
                        trigger_type="single",
                        trigger_values=[decision.stop_loss_price],
                        last_price=fill_price,
                        orders=[{
                            "exchange": "NSE",
                            "tradingsymbol": signal.stock,
                            "transaction_type": "SELL",
                            "quantity": decision.adjusted_qty,
                            "order_type": "MARKET",
                            "product": decision.product_type.value,
                        }],
                    )
                    gtt_trigger_id = gtt_resp.get("trigger_id") if isinstance(gtt_resp, dict) else None
                    if gtt_trigger_id:
                        logger.info("GTT placed for %s: trigger_id=%s", signal.stock, gtt_trigger_id)
                except Exception as gtt_exc:
                    logger.error("GTT placement failed for %s: %s", signal.stock, gtt_exc)

        except Exception as exc:
            logger.error("Order placement failed for %s: %s", signal.stock, exc)
            return

        # Persist trade to DB
        async with get_db_context() as session:
            trade = Trade(
                kite_order_id=str(order_id),
                stock=signal.stock,
                exchange=signal.exchange,
                direction="BUY",
                product_type=decision.product_type.value,
                quantity=decision.adjusted_qty,
                entry_price=fill_price,
                stop_loss_price=decision.stop_loss_price,
                target_price=decision.target_price,
                status="OPEN",
                trade_date=now_ist.date(),
                entry_time=now_ist.time(),
                decision_rationale=decision.rationale,
                gtt_trigger_id=gtt_trigger_id if not settings.paper_trading else None,
            )
            session.add(trade)

        # Increment daily trade counter
        await increment(TRADE_COUNT_KEY)

        # Publish trade event to Redis
        await publish("trade_events", {
            "type": "TRADE_OPENED",
            "stock": signal.stock,
            "price": fill_price,
            "stop_loss": decision.stop_loss_price,
            "target": decision.target_price,
            "qty": decision.adjusted_qty,
            "product_type": decision.product_type.value,
            "timestamp": now_ist.isoformat(),
        })

        # Telegram alert
        await send_trade_entry_alert(
            stock=signal.stock,
            price=fill_price,
            sl=decision.stop_loss_price,
            target=decision.target_price,
            qty=decision.adjusted_qty,
            product_type=decision.product_type.value,
        )

        logger.info(
            "✅ Trade executed: BUY %s @ ₹%.2f | SL: ₹%.2f | TGT: ₹%.2f | Qty: %d | %s",
            signal.stock, fill_price, decision.stop_loss_price,
            decision.target_price, decision.adjusted_qty, decision.product_type.value,
        )

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
        loop = asyncio.get_event_loop()
        self._risk_manager.start(loop)

        # Start Scanner (WebSocket) and Decision processing concurrently
        # with supervision: if either crashes, log + alert and stop cleanly.
        scanner_task = asyncio.create_task(self._scanner.start(), name="scanner")
        decision_task = asyncio.create_task(self._process_decisions(), name="decision_processor")

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

    async def stop(self) -> None:
        """Stop all sub-modules."""
        self._running = False
        self._scanner.stop()
        self._decision_engine.stop()
        self._risk_manager.stop()
        await set_value("agent:trading:status", "INACTIVE")
        logger.info("═══ Trading Agent stopped ═══")
