"""
health.py
Monitoreo de vida del bot: heartbeat local + dead-man's switch externo.

Dos mecanismos complementarios:

1. HEARTBEAT LOCAL (`beat()`): escribe el epoch actual en un archivo
   (data/heartbeat). El HEALTHCHECK de Docker lee ese archivo y, si está viejo
   (el loop se colgó), marca el container como "unhealthy". Visibilidad en
   `docker ps`.

2. DEAD-MAN'S SWITCH EXTERNO (`ping()`): si HEALTHCHECK_URL está seteado
   (ej. una URL de healthchecks.io), el bot le pega un GET en cada ciclo.
   Si el ping DEJA de llegar (VPS apagado, Docker muerto, red caída o loop
   colgado), el servicio externo te alerta a vos. Es lo único que detecta la
   muerte del bot DESDE AFUERA — un watchdog interno no puede avisarte si todo
   el proceso/host se cayó.

Ninguna función raisea: el monitoreo nunca debe tumbar el bot.
"""

import os
import time
import logging
from pathlib import Path

import requests

logger = logging.getLogger("health")

HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "data/heartbeat"))
_HC_URL = os.environ.get("HEALTHCHECK_URL", "").strip()
_PING_TIMEOUT = int(os.environ.get("HEALTHCHECK_PING_TIMEOUT", "8"))


def beat(ts: float | None = None) -> None:
    """
    Escribe el timestamp actual (epoch entero) al heartbeat local de forma
    atómica (tmp + rename) para que el healthcheck nunca lea un archivo a medias.
    """
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.write_text(str(int(ts if ts is not None else time.time())))
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception as e:
        logger.warning(f"health: no pude escribir heartbeat: {e}")


def ping(suffix: str = "") -> bool:
    """
    Pinga el dead-man's switch externo si HEALTHCHECK_URL está configurado.
      suffix=""       → ciclo OK
      suffix="/start" → arranque del bot
      suffix="/fail"  → fallo/crash (el servicio alerta de inmediato)
    Devuelve True si el ping salió, False si no hay URL o falló (nunca raisea).
    """
    if not _HC_URL:
        return False
    url = _HC_URL.rstrip("/") + suffix
    try:
        requests.get(url, timeout=_PING_TIMEOUT)
        return True
    except Exception as e:
        logger.warning(f"health: ping a healthcheck falló: {e}")
        return False


def enabled() -> bool:
    """True si el dead-man's switch externo está configurado."""
    return bool(_HC_URL)


def seconds_since_beat() -> float | None:
    """Antigüedad del último heartbeat en segundos, o None si no existe/ilegible."""
    try:
        return time.time() - float(HEARTBEAT_FILE.read_text().strip())
    except Exception:
        return None
