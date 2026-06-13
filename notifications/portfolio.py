"""
notifications/portfolio.py
Lectura agregada del estado de los bots multi-asset para la vista de portafolio
en Telegram.

Cada bot escribe su estado en data/<sym>/state.json (volumen propio por container).
El bot que corre el listener de Telegram puede montar el directorio padre
(read-only) y, con PORTFOLIO_DATA_DIR apuntando a él, leer el estado de los 3.

Funciones puras (solo filesystem) → testeables sin red ni exchange. Los precios
en vivo y el render los pone el listener, que sí tiene acceso al exchange.
"""

import json
from pathlib import Path
from typing import Optional


def dir_to_symbol(name: str) -> str:
    """'btc' -> 'BTC/USDT'. Mapea el nombre del directorio del bot a su par."""
    return f"{name.upper()}/USDT"


def discover_bot_dirs(base_dir) -> list[Path]:
    """Subdirectorios de base_dir que contienen un state.json, ordenados."""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    return sorted(
        c for c in base.iterdir()
        if c.is_dir() and (c / "state.json").exists()
    )


def load_bot_state(state_file) -> Optional[dict]:
    """
    Lee un state.json. Devuelve None si no se pudo (escritura concurrente del
    otro bot, JSON inválido, etc.) — el caller lo muestra como 'n/d'.
    """
    try:
        return json.loads(Path(state_file).read_text(encoding="utf-8"))
    except Exception:
        return None


def load_portfolio(base_dir) -> list[dict]:
    """
    Devuelve [{name, symbol, state}] para cada bot encontrado bajo base_dir.
    `state` puede ser None si no se pudo leer en este instante.
    """
    out: list[dict] = []
    for d in discover_bot_dirs(base_dir):
        out.append({
            "name": d.name,
            "symbol": dir_to_symbol(d.name),
            "state": load_bot_state(d / "state.json"),
        })
    return out


def position_pnl(pos: dict, price: float) -> tuple[float, float]:
    """
    PnL en vivo (USDT, %) de una posición dada el precio actual.
    pos: dict con entry_price, amount_btc, direction.
    """
    entry = float(pos.get("entry_price", 0) or 0)
    amt = float(pos.get("amount_btc", 0) or 0)
    direction = pos.get("direction", "LONG")
    if entry <= 0 or amt <= 0 or price <= 0:
        return 0.0, 0.0
    if direction == "SHORT":
        pnl = (entry - price) * amt
        pct = (entry - price) / entry * 100
    else:
        pnl = (price - entry) * amt
        pct = (price - entry) / entry * 100
    return pnl, pct
