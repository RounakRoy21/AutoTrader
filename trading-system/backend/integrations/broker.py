"""
BrokerPort — the one order / position interface the trading code talks to
(audit Part B D1).

Two implementations satisfy it:

  * ``GrowwBroker`` (integrations/groww_broker.py) — live orders via Groww.
  * ``PaperBroker`` (integrations/paper_broker.py) — in-memory simulation that
    exercises the SAME order paths (fill confirmation, resting stop, cancel +
    confirm, positions) instead of skipping them in paper mode (audit V8).

The shapes are the normalised ones already produced by ``GrowwClient`` (and
consumed by ``agents.order_lifecycle``):

  * ``get_order_state`` → ``{"order_id", "status", "filled_quantity",
    "quantity", "average_price"}``; status is COMPLETE / REJECTED / CANCELLED
    (terminal) or a raw pending status.
  * ``get_positions``   → ``{"net": [{"tradingsymbol", "exchange", "quantity"
    (signed), "average_price", "product"}]}``.
  * ``get_orders``      → today's raw order-book rows (``groww_order_id``,
    ``trading_symbol``, ``transaction_type``, ``order_type``,
    ``order_status``, ``average_fill_price`` ...).
  * ``get_ltp``         → ``{"NSE:SYM": {"last_price": float}}``; missing /
    invalid prices are OMITTED, never 0.0; a transport failure raises.
  * ``get_quote``       → ``{"NSE:SYM": {"last_price", "upper_circuit_limit",
    "lower_circuit_limit"}}``; unknown values are None.

Error contract: order writes are sent once.  An ambiguous write raises
``OrderStateUnknownError`` (the order MAY be live); any other exception is a
definitive failure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from integrations.groww_client import OrderStateUnknownError

__all__ = ["BrokerPort", "OrderStateUnknownError", "get_broker"]


@runtime_checkable
class BrokerPort(Protocol):
    async def place_order(
        self,
        tradingsymbol: str,
        exchange: str = "NSE",
        transaction_type: str = "BUY",
        quantity: int = 1,
        product: str = "MIS",
        order_type: str = "MARKET",
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        tag: str = "autotrader",
        order_reference_id: Optional[str] = None,
    ) -> str: ...

    async def place_stop_loss_order(
        self,
        tradingsymbol: str,
        quantity: int,
        trigger_price: float,
        exchange: str = "NSE",
        product: str = "MIS",
        transaction_type: str = "SELL",
    ) -> str: ...

    async def modify_stop_loss_order(
        self, order_id: str, tradingsymbol: str, quantity: int, trigger_price: float,
    ) -> None: ...

    async def cancel_order(self, order_id: str) -> None: ...

    async def get_order_state(self, order_id: str) -> Dict[str, Any]: ...

    async def find_order_by_reference(self, order_reference_id: str) -> Optional[str]: ...

    async def get_positions(self) -> Dict[str, Any]: ...

    async def get_orders(self) -> List[Dict[str, Any]]: ...

    async def get_ltp(self, instruments: List[str]) -> Dict[str, Any]: ...

    async def get_quote(self, instruments: List[str]) -> Dict[str, Any]: ...


def get_broker(settings: Any = None) -> BrokerPort:
    """The broker for the current mode: ``PaperBroker`` when paper trading,
    otherwise ``GrowwBroker``.  Both are process-wide singletons."""
    if settings is None:
        from core.config import get_settings

        settings = get_settings()
    if settings.paper_trading:
        from integrations.paper_broker import get_paper_broker

        return get_paper_broker()
    from integrations.groww_broker import get_groww_broker

    return get_groww_broker()
