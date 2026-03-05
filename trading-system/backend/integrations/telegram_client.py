"""
Telegram Bot wrapper — sends alerts and EOD reports to the configured chat.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 10

# Module-level singleton — reuses the TCP connection pool across all alerts.
# Under a stop-loss cascade this avoids spinning up N parallel TCP connections
# to the Telegram API.
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    """Return the module-level HTTP client, creating it if necessary."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=TIMEOUT)
    return _http_client


async def send_telegram(message: str) -> bool:
    """
    Send a Telegram message to the configured chat.
    Returns True if successful, False otherwise.
    """
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram not configured — skipping alert")
        return False

    url = TELEGRAM_API_BASE.format(token=settings.telegram_bot_token)
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        client = _get_http_client()
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        logger.info("Telegram alert sent: %s", message[:80])
        return True
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


async def send_trade_entry_alert(
    stock: str, price: float, sl: float, target: float, qty: int, product_type: str
) -> bool:
    """Send a formatted trade entry alert."""
    msg = (
        f"🟢 BUY {stock} @ ₹{price:.2f} | "
        f"SL: ₹{sl:.2f} | Target: ₹{target:.2f} | "
        f"Qty: {qty} | {product_type}"
    )
    return await send_telegram(msg)


async def send_stop_loss_alert(stock: str, ltp: float, loss: float) -> bool:
    """Send a stop-loss hit alert."""
    msg = f"🔴 STOP LOSS HIT: {stock} @ ₹{ltp:.2f} | Loss: ₹{loss:.2f}"
    return await send_telegram(msg)


async def send_target_hit_alert(stock: str, ltp: float, profit: float) -> bool:
    """Send a target hit alert."""
    msg = f"✅ TARGET HIT: {stock} @ ₹{ltp:.2f} | Profit: ₹{profit:.2f}"
    return await send_telegram(msg)


async def send_halt_alert() -> bool:
    """Send a daily loss limit breach alert."""
    return await send_telegram("🚨 DAILY LOSS LIMIT HIT. Trading halted for the day.")


async def send_intraday_close_alert() -> bool:
    """Send an intraday square-off notification."""
    return await send_telegram("🕒 Intraday close initiated. Squaring off all MIS positions.")


async def send_eod_report(
    total_trades: int, won: int, lost: int, net_pnl: float, return_pct: float
) -> bool:
    """Send the End of Day summary report."""
    msg = (
        f"📊 EOD Report | Trades: {total_trades} | "
        f"Won: {won} | Lost: {lost} | "
        f"Net P&L: ₹{net_pnl:.2f} | Return: {return_pct:.2f}%"
    )
    return await send_telegram(msg)
