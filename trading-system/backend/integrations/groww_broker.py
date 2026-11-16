"""
GrowwBroker — the live ``BrokerPort`` implementation (audit Part B D1).

A thin adapter over ``GrowwClient``: it exposes exactly the port surface and
nothing else.  The client keeps the
Groww-specific work: tick snapping, send-once writes with an
``order_reference_id``, reference lookup on ambiguous failures and response
normalisation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from integrations.groww_client import GrowwClient, get_groww_client


class GrowwBroker:
    def __init__(self, client: Optional[GrowwClient] = None) -> None:
        self._client = client

    @property
    def client(self) -> GrowwClient:
        return self._client or get_groww_client()

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
    ) -> str:
        return await self.client.place_order(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            tag=tag,
            order_reference_id=order_reference_id,
        )

    async def place_stop_loss_order(
        self,
        tradingsymbol: str,
        quantity: int,
        trigger_price: float,
        exchange: str = "NSE",
        product: str = "MIS",
        transaction_type: str = "SELL",
    ) -> str:
        return await self.client.place_stop_loss_order(
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            trigger_price=trigger_price,
            exchange=exchange,
            product=product,
            transaction_type=transaction_type,
        )

    async def modify_stop_loss_order(
        self, order_id: str, tradingsymbol: str, quantity: int, trigger_price: float,
    ) -> None:
        await self.client.modify_stop_loss_order(
            order_id=order_id, tradingsymbol=tradingsymbol,
            quantity=quantity, trigger_price=trigger_price,
        )

    async def cancel_order(self, order_id: str) -> None:
        await self.client.cancel_order(order_id)

    async def get_order_state(self, order_id: str) -> Dict[str, Any]:
        return await self.client.get_order_state(order_id)

    async def find_order_by_reference(self, order_reference_id: str) -> Optional[str]:
        return await self.client.find_order_by_reference(order_reference_id)

    async def get_positions(self) -> Dict[str, Any]:
        return await self.client.get_positions()

    async def get_orders(self) -> List[Dict[str, Any]]:
        return await self.client.get_orders()

    async def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        return await self.client.get_ltp(instruments)

    async def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        return await self.client.get_quote(instruments)


_groww_broker: Optional[GrowwBroker] = None


def get_groww_broker() -> GrowwBroker:
    global _groww_broker
    if _groww_broker is None:
        _groww_broker = GrowwBroker()
    return _groww_broker
