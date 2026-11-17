"""Gestión de riesgo determinista. La usan el backtest y el bot por igual.

Nada de esto se decide con un modelo de lenguaje ni con configuración externa: los límites son constantes de
`quant.config` y la lógica es aritmética simple, testeada en `quant/tests`.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from quant import config as C


def floor_step(x: float, step: float = C.QTY_STEP) -> float:
    return math.floor(x / step + 1e-9) * step


@dataclass
class SizeDecision:
    qty: float
    reason: str = "ok"

    @property
    def ok(self) -> bool:
        return self.qty > 0


def position_size(equity: float, entry: float, stop: float) -> SizeDecision:
    """Cantidad tal que (entry − stop) × qty ≈ RISK_PER_TRADE × equity, con tope de nocional sin apalancamiento.
    Los costos de salida por stop se suman a la distancia para que el riesgo real no supere el 1 %."""
    if not (equity > 0 and entry > 0 and stop > 0):
        return SizeDecision(0.0, "invalid_inputs")
    if stop >= entry:
        return SizeDecision(0.0, "stop_above_entry")
    dist_pct = (entry - stop) / entry
    if dist_pct < C.MIN_STOP_DIST_PCT:
        return SizeDecision(0.0, "stop_too_tight")
    if dist_pct > C.MAX_STOP_DIST_PCT:
        return SizeDecision(0.0, "stop_too_wide")
    loss_per_unit = (entry - stop) + stop * (C.FEE_RATE + C.SLIPPAGE_RATE) + entry * C.FEE_RATE
    qty = C.RISK_PER_TRADE * equity / loss_per_unit
    qty = min(qty, C.MAX_POSITION_PCT * equity / (entry * (1 + C.FEE_RATE)))
    qty = floor_step(qty)
    if qty * entry < C.MIN_NOTIONAL_USDT:
        return SizeDecision(0.0, "below_min_notional")
    return SizeDecision(qty)


@dataclass
class RiskState:
    """Estado de los kill-switches. Serializable para sobrevivir reinicios del bot."""
    peak_equity: float = C.INITIAL_CAPITAL_USDT
    day: str = ""                      # fecha UTC YYYY-MM-DD
    day_start_equity: float = C.INITIAL_CAPITAL_USDT
    daily_locked: bool = False         # pérdida diaria alcanzada → sin entradas hasta el próximo día UTC
    halted: bool = False               # drawdown máximo alcanzado → apagado hasta reset manual
    events: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class RiskManager:
    def __init__(self, state: RiskState | None = None):
        self.s = state or RiskState()

    def on_new_day(self, day: str, equity: float) -> None:
        if day != self.s.day:
            self.s.day = day
            self.s.day_start_equity = equity
            self.s.daily_locked = False

    def check(self, equity: float, when: str = "") -> str | None:
        """Evalúa el equity marcado a mercado. Devuelve la acción obligatoria:
        'halt' (cerrar todo y apagarse), 'flatten_day' (cerrar todo, sin entradas hasta mañana) o None."""
        self.s.peak_equity = max(self.s.peak_equity, equity)
        if not self.s.halted and equity <= self.s.peak_equity * (1 - C.MAX_DRAWDOWN_HALT):
            self.s.halted = True
            self.s.events.append({"type": "halt", "when": when, "equity": equity, "peak": self.s.peak_equity})
            return "halt"
        if not self.s.daily_locked and equity <= self.s.day_start_equity * (1 - C.MAX_DAILY_LOSS):
            self.s.daily_locked = True
            self.s.events.append({"type": "daily_loss", "when": when, "equity": equity,
                                  "day_start": self.s.day_start_equity})
            return "flatten_day"
        return None

    def can_enter(self) -> tuple[bool, str]:
        if self.s.halted:
            return False, "halted_max_drawdown"
        if self.s.daily_locked:
            return False, "daily_loss_lock"
        return True, "ok"
