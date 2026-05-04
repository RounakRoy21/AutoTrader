"""
Telegram Bot wrapper — sends alerts and EOD reports to the configured chat.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dt_time
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
    stock: str,
    price: float,
    sl: float,
    target: float,
    qty: int,
    product_type: str,
    rationale: str = "",
) -> bool:
    """Send a formatted trade entry alert with R:R and risk metrics."""
    risk_per_share = price - sl
    reward_per_share = target - price
    risk_total = risk_per_share * qty
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else 0.0
    sl_pct = (risk_per_share / price) * 100 if price > 0 else 0.0
    tgt_pct = (reward_per_share / price) * 100 if price > 0 else 0.0

    lines = [
        f"🟢 <b>BUY {stock}</b>  ·  {product_type}",
        f"Entry ₹{price:.2f}  ·  Qty {qty}",
        f"SL ₹{sl:.2f} <i>(-{sl_pct:.1f}%)</i>  →  Target ₹{target:.2f} <i>(+{tgt_pct:.1f}%)</i>",
        f"Risk ₹{risk_total:.0f}  ·  R:R 1 : {rr:.1f}",
    ]
    if rationale:
        # Trim rationale to keep the message readable on mobile
        short = rationale[:120].rstrip()
        if len(rationale) > 120:
            short += "…"
        lines.append(f"💬 <i>{short}</i>")

    return await send_telegram("\n".join(lines))


async def send_stop_loss_alert(
    stock: str,
    ltp: float,
    loss: float,
    entry_price: float = 0.0,
    entry_time: Optional[dt_time] = None,
    quantity: int = 0,
) -> bool:
    """Send a stop-loss hit alert with entry context and hold duration."""
    drop_pct = ((ltp - entry_price) / entry_price * 100) if entry_price > 0 else 0.0

    lines = [f"🔴 <b>STOP LOSS HIT: {stock}</b>"]
    if entry_price > 0:
        lines.append(f"Entry ₹{entry_price:.2f}  →  Exit ₹{ltp:.2f}  <i>({drop_pct:+.1f}%)</i>")
    else:
        lines.append(f"Exit @ ₹{ltp:.2f}")

    detail = f"Loss ₹{loss:.2f}"
    if entry_time is not None:
        now_time = datetime.now().time()
        # Compute duration using today's date for both entry and exit
        from datetime import date
        today = date.today()
        entry_dt = datetime.combine(today, entry_time)
        exit_dt = datetime.combine(today, now_time)
        held_min = (exit_dt - entry_dt).total_seconds() / 60
        if held_min >= 0:
            detail += f"  ·  Held {held_min:.0f} min"
    lines.append(detail)

    return await send_telegram("\n".join(lines))


async def send_target_hit_alert(
    stock: str,
    ltp: float,
    profit: float,
    entry_price: float = 0.0,
    entry_time: Optional[dt_time] = None,
    quantity: int = 0,
) -> bool:
    """Send a target hit alert with entry context and hold duration."""
    gain_pct = ((ltp - entry_price) / entry_price * 100) if entry_price > 0 else 0.0

    lines = [f"✅ <b>TARGET HIT: {stock}</b>"]
    if entry_price > 0:
        lines.append(f"Entry ₹{entry_price:.2f}  →  Exit ₹{ltp:.2f}  <i>(+{gain_pct:.1f}%)</i>")
    else:
        lines.append(f"Exit @ ₹{ltp:.2f}")

    detail = f"Profit ₹{profit:.2f}"
    if entry_time is not None:
        from datetime import date
        today = date.today()
        entry_dt = datetime.combine(today, entry_time)
        exit_dt = datetime.combine(today, datetime.now().time())
        held_min = (exit_dt - entry_dt).total_seconds() / 60
        if held_min >= 0:
            detail += f"  ·  Held {held_min:.0f} min"
    lines.append(detail)

    return await send_telegram("\n".join(lines))


async def send_halt_alert(
    total_loss: Optional[float] = None,
    drawdown_pct: Optional[float] = None,
) -> bool:
    """Send a daily loss limit breach alert with loss amount and percentage."""
    lines = ["🚨 <b>TRADING HALTED</b>"]
    if total_loss is not None and drawdown_pct is not None:
        lines.append(f"Daily loss limit breached: ₹{total_loss:.2f} ({drawdown_pct:.1f}% of capital)")
    else:
        lines.append("Daily loss limit breached.")
    lines.append("All new trades blocked for today.")
    return await send_telegram("\n".join(lines))


async def send_intraday_close_alert(
    n_positions: int = 0,
    running_pnl: Optional[float] = None,
) -> bool:
    """Send an intraday square-off notification."""
    pos_str = f"{n_positions} position{'s' if n_positions != 1 else ''}" if n_positions > 0 else "all MIS positions"
    lines = [f"🕒 <b>MIS Square-off</b>  ·  Closing {pos_str}"]
    if running_pnl is not None:
        sign = "+" if running_pnl >= 0 else ""
        lines.append(f"Running P&L at close: ₹{sign}{running_pnl:.2f}")
    return await send_telegram("\n".join(lines))


async def send_eod_report(
    total_trades: int,
    won: int,
    lost: int,
    net_pnl: float,
    return_pct: float,
    losses_before_1030: int = 0,
    losses_1030_to_1330: int = 0,
    losses_after_1330: int = 0,
    profit_factor: float = 0.0,
    sharpe: float = 0.0,
    avg_realised_rr: float = 0.0,
    avg_duration: float = 0.0,
    max_consec_losses: int = 0,
    halted: bool = False,
) -> bool:
    """Send the End of Day summary report with full session metrics."""
    win_rate = round(won / total_trades * 100) if total_trades > 0 else 0
    pnl_sign = "+" if net_pnl >= 0 else ""
    emoji = "🟢" if net_pnl >= 0 else "🔴"
    halted_str = "  🚨 <b>HALTED</b>" if halted else ""

    lines = [
        f"📊 <b>EOD Report</b>{halted_str}",
        f"{emoji} Net P&L: ₹{pnl_sign}{net_pnl:.2f}  <i>({pnl_sign}{return_pct:.2f}%)</i>",
        "",
        f"Trades {total_trades}  ·  W {won}  /  L {lost}  ·  Win rate {win_rate}%",
    ]

    if total_trades > 0:
        metrics: list[str] = []
        if profit_factor > 0:
            pf_str = "∞" if profit_factor >= 999 else f"{profit_factor:.2f}"
            metrics.append(f"PF {pf_str}")
        if avg_realised_rr > 0:
            rr_str = "∞" if avg_realised_rr >= 999 else f"{avg_realised_rr:.2f}"
            metrics.append(f"Avg R:R {rr_str}")
        if sharpe != 0:
            metrics.append(f"Sharpe {sharpe:.2f}")
        if metrics:
            lines.append("  ·  ".join(metrics))

        dur_parts: list[str] = []
        if avg_duration > 0:
            dur_parts.append(f"Avg hold {avg_duration:.0f} min")
        if max_consec_losses > 0:
            dur_parts.append(f"Max consec. losses {max_consec_losses}")
        if dur_parts:
            lines.append("  ·  ".join(dur_parts))

    total_losses = losses_before_1030 + losses_1030_to_1330 + losses_after_1330
    if total_losses > 0:
        lines.append(
            f"⏰ Loss timing:  pre-10:30 {losses_before_1030}"
            f"  ·  10:30–13:30 {losses_1030_to_1330}"
            f"  ·  post-13:30 {losses_after_1330}"
        )

    return await send_telegram("\n".join(lines))
