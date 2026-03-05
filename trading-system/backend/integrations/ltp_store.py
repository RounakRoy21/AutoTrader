"""
Thread-safe in-memory LTP (Last Traded Price) store.

The Scanner writes here on every tick; the RiskManager reads from here
in paper trading mode (where real Kite LTP calls aren't possible).
Uses a simple RLock so it is safe to call from both the asyncio event
loop (Scanner / paper mode) and the RiskManager daemon thread.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional

_lock = threading.RLock()
_store: Dict[str, float] = {}


def set_ltp(symbol: str, price: float) -> None:
    """Update the LTP for a symbol."""
    with _lock:
        _store[symbol] = price


def get_ltp(symbol: str) -> Optional[float]:
    """Return the most recent LTP for a symbol, or None if not yet received."""
    with _lock:
        return _store.get(symbol)


def get_all() -> Dict[str, float]:
    """Return a snapshot of all current LTPs."""
    with _lock:
        return dict(_store)


def clear() -> None:
    """Clear all stored LTPs (called at session start)."""
    with _lock:
        _store.clear()
