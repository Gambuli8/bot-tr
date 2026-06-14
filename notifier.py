"""
notifier.py
Wrapper simple para alertas Telegram. Lee TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
del entorno. No usa asyncio (requests sync, idempotente).

Uso:
    from notifier import send_message
    send_message("🟢 BTC LONG abierto @ $63,400")

Si las env vars no están seteadas, send_message() loguea localmente y no
revienta (modo desarrollo). En producción setealas vía .env y Docker.
"""

import os
import logging
from typing import Optional

import requests

logger = logging.getLogger("notifier")


def _esc(s) -> str:
    """Escapa texto dinámico para parse_mode=HTML (evita HTTP 400 con '<='/'>=')."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BOT_TOKEN: Optional[str] = None
_CHAT_ID: Optional[str] = None
_API_URL: Optional[str] = None


def _ensure_loaded() -> bool:
    """Carga creds del entorno la primera vez. Cachea para evitar reads."""
    global _BOT_TOKEN, _CHAT_ID, _API_URL
    if _BOT_TOKEN is not None:
        return bool(_BOT_TOKEN and _CHAT_ID)
    _BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    _CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not _BOT_TOKEN or not _CHAT_ID:
        logger.warning(
            "notifier: TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no seteados. "
            "Los mensajes se loguearán localmente."
        )
        return False
    _API_URL = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    return True


def send_message(text: str, parse_mode: str = "HTML", timeout: int = 8) -> bool:
    """
    Envía text como mensaje a Telegram. Devuelve True si llegó (HTTP 200),
    False si falló o si no hay creds. Nunca raisea.
    """
    if not _ensure_loaded():
        logger.info(f"[telegram-stub] {text}")
        return False
    try:
        r = requests.post(
            _API_URL,
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning(
                f"notifier: HTTP {r.status_code} de Telegram → {r.text[:200]}"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"notifier: excepción al enviar Telegram: {e}")
        return False


# ─────────────────────────────────────────
#  Helpers tipados para eventos críticos
# ─────────────────────────────────────────

def notify_open(symbol: str, direction: str, entry_price: float,
                amount_usdt: float, sl: float, tp: float) -> bool:
    """Notificación de apertura."""
    emoji = "🟢" if direction == "LONG" else "🔻"
    text = (
        f"{emoji} <b>OPERACIÓN ABIERTA</b>\n\n"
        f"📊 Par: <b>{symbol}</b>\n"
        f"📈 Dirección: <b>{direction}</b>\n"
        f"💵 Precio entrada: <b>${entry_price:,.2f}</b>\n"
        f"💼 Notional: <b>${amount_usdt:,.2f}</b>\n"
        f"🛑 Stop-Loss: <b>${sl:,.2f}</b>\n"
        f"🎯 Take-Profit: <b>${tp:,.2f}</b>"
    )
    return send_message(text)


def notify_close(symbol: str, direction: str, exit_price: float,
                 pnl_usdt: float, pnl_pct: float, balance: float,
                 reason: str = "") -> bool:
    """Notificación de cierre con PnL y balance total."""
    emoji = "🎉" if pnl_usdt > 0 else "😔"
    sign = "+" if pnl_usdt >= 0 else ""
    text = (
        f"{emoji} <b>OPERACIÓN CERRADA</b>\n\n"
        f"📊 Par: <b>{symbol}</b>  ({direction})\n"
        f"💵 Precio salida: <b>${exit_price:,.2f}</b>\n"
        f"📊 PnL: <b>{sign}${pnl_usdt:,.2f}</b>  ({sign}{pnl_pct:.2f}%)\n"
        f"💰 Balance total: <b>${balance:,.2f}</b>"
        + (f"\n📝 Motivo: <i>{_esc(reason)}</i>" if reason else "")
    )
    return send_message(text)


def notify_critical(message: str) -> bool:
    """Excepciones críticas o fallos de conexión."""
    text = f"🚨 <b>ERROR CRÍTICO</b>\n\n{_esc(message[:1000])}"
    return send_message(text)
