"""
Payload que manda TradingView (Pine) en cada alerta.

Un setup avanza por etapas; cada etapa manda un evento para que el bot vaya
contando qué está analizando:

  zone    → 1D: el precio llegó a una zona de soporte/resistencia y frenó.
  choch   → 1H: cambio de estructura confirmado, se traza Fibonacci del impulso.
  fib     → 1H: el retroceso llegó a 0.618 sin romper 0.75. Se espera el gatillo en 5m.
  cancel  → el setup se invalidó (rompió 0.75, venció, etc.).
  entry   → 5m: rompió la diagonal del retroceso. ORDEN.

Ejemplo de `entry`:
{
  "secret": "…", "event": "entry", "id": "BTCUSDT-L-1789585800000",
  "symbol": "BTCUSDT.P", "side": "LONG", "price": 75832.5,
  "sl": 74950.0, "tp": 78200.0,
  "fib_start": 73100.0, "fib_end": 78200.0, "fib_618": 75048.2, "fib_75": 74375.0,
  "zone_low": 72800.0, "zone_high": 73400.0,
  "time": 1789585800000, "note": "ruptura diagonal 5m"
}
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from bot.config import normalize_symbol

EventType = Literal["zone", "choch", "fib", "cancel", "entry"]


class Signal(BaseModel):
    secret: str = Field(default="", repr=False)
    event: EventType
    id: str = Field(min_length=1, max_length=120)
    symbol: str
    side: Literal["LONG", "SHORT"]
    price: float = Field(gt=0)
    time: Optional[int] = None  # ms epoch de la vela que disparó

    sl: Optional[float] = None
    tp: Optional[float] = None

    fib_start: Optional[float] = None  # inicio del impulso (nivel 1.0)
    fib_end: Optional[float] = None    # fin del impulso (nivel 0.0) = TP natural
    fib_618: Optional[float] = None
    fib_75: Optional[float] = None
    zone_low: Optional[float] = None
    zone_high: Optional[float] = None
    note: str = Field(default="", max_length=300)

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return normalize_symbol(v)

    @field_validator("side", mode="before")
    @classmethod
    def _norm_side(cls, v: str) -> str:
        v = str(v).upper()
        return {"BUY": "LONG", "SELL": "SHORT"}.get(v, v)

    @model_validator(mode="after")
    def _entry_needs_brackets(self) -> "Signal":
        if self.event == "entry" and (self.sl is None or self.tp is None):
            raise ValueError("un evento 'entry' necesita sl y tp")
        return self

    @property
    def base_asset(self) -> str:
        return self.symbol.split("-")[0]

    def client_order_id(self) -> str:
        """clientOrderID de BingX: máx 40 chars, alfanumérico/guiones."""
        safe = "".join(ch for ch in self.id if ch.isalnum() or ch in "-_")
        return ("tv-" + safe)[-40:]
