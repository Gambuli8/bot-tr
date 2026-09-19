"""
Formato de números para los mensajes (estilo argentino):
miles con punto, decimales con coma, signo explícito donde importa.

  money(100000)          → $100.000,00
  money(0.335, True)     → +$0,335
  money(-0.122, True)    → −$0,122
  price(76014.1)         → $76.014,1
  pct(-0.68, True)       → −0,68%
"""

from __future__ import annotations

from typing import Optional

MINUS = "−"  # signo menos tipográfico (se lee mejor que el guion)


def num(value: float, decimals: int = 2) -> str:
    text = f"{abs(value):,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    negative = value < 0 and round(abs(value), decimals) != 0
    return (MINUS if negative else "") + text


def _signed(value: float, decimals: int, body: str, signed: bool) -> str:
    if round(abs(value), decimals) == 0:
        return body
    if value < 0:
        return MINUS + body
    return ("+" + body) if signed else body


def money(value: Optional[float], signed: bool = False) -> str:
    """Montos: 2 decimales; 3 si es menor a 1 (las operaciones son de centavos)."""
    if value is None:
        return "—"
    a = abs(value)
    decimals = 2 if a >= 1 or a == 0 else 3 if a >= 0.001 else 4
    return _signed(value, decimals, "$" + num(a, decimals), signed)


def price(value: Optional[float]) -> str:
    """Precio con decimales según magnitud: $76.014,1 · $2.392,40 · $98,379 · $1,2922 · $0,08059."""
    if value is None:
        return "—"
    v = float(value)
    a = abs(v)
    decimals = 1 if a >= 10000 else 2 if a >= 100 else 3 if a >= 10 else 4 if a >= 1 else 5
    return "$" + num(v, decimals)


def pct(value: Optional[float], signed: bool = False, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return _signed(value, decimals, num(abs(value), decimals) + "%", signed)


def ratio(value: float, decimals: int = 1) -> str:
    return num(value, decimals)


def qty(value: float) -> str:
    """Cantidad sin ceros de más: 0,0001 · 49 · 1.250,5."""
    text = f"{abs(value):.8f}".rstrip("0").rstrip(".")
    integer, _, frac = text.partition(".")
    body = num(float(integer), 0) + ("," + frac if frac else "")
    return (MINUS + body) if value < 0 else body
