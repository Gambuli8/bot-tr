"""Broker de paper trading: precios reales de Binance, órdenes simuladas con el MISMO modelo de costos del backtest.

El stop se simula como una orden stop que vive en el exchange: aunque el bot pierda la conexión una hora, al
volver revisa las velas de 1 minuto del período caído y ejecuta el stop donde realmente lo habría hecho
(al stop, o a la apertura de la vela si hubo gap), con slippage y comisión.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from quant import config as C
from quant.risk import RiskState, position_size


@dataclass
class Position:
    qty: float
    entry_px: float
    stop: float
    entry_fee: float
    entry_ts: int
    entry_equity: float
    init_risk: float


@dataclass
class PaperState:
    cash: float = C.INITIAL_CAPITAL_USDT
    position: dict | None = None
    last_bar_ts: int = 0            # última vela de 4h procesada
    last_check_ms: int = 0          # hasta dónde se revisaron las velas de 1m para el stop
    risk: dict = field(default_factory=lambda: RiskState().to_dict())
    trades: list = field(default_factory=list)

    def pos(self) -> Position | None:
        return Position(**self.position) if self.position else None


def save_state(state: PaperState, path: Path = C.STATE_PATH) -> None:
    """Escritura atómica: un corte de luz a mitad de escritura no deja un JSON roto."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_state(path: Path = C.STATE_PATH) -> PaperState:
    if not path.exists():
        return PaperState()
    d = json.loads(path.read_text(encoding="utf-8"))
    return PaperState(**{k: v for k, v in d.items() if k in PaperState.__dataclass_fields__})


class PaperBroker:
    def __init__(self, state: PaperState):
        self.s = state

    def equity(self, mark: float) -> float:
        p = self.s.pos()
        return self.s.cash + (p.qty * mark if p else 0.0)

    def buy(self, ref_px: float, stop_dist: float, ts_ms: int) -> tuple[bool, str]:
        if self.s.position:
            return False, "already_in_position"
        px = ref_px * (1 + C.SLIPPAGE_RATE)
        stop = px - stop_dist
        d = position_size(self.s.cash, px, stop)
        if not d.ok:
            return False, d.reason
        fee = d.qty * px * C.FEE_RATE
        equity_before = self.s.cash
        self.s.cash -= d.qty * px + fee
        self.s.position = asdict(Position(d.qty, px, stop, fee, ts_ms, equity_before, d.qty * (px - stop)))
        self.s.last_check_ms = ts_ms
        return True, f"qty={d.qty:.5f} px={px:.2f} stop={stop:.2f}"

    def sell(self, ref_px: float, ts_ms: int, reason: str) -> dict | None:
        p = self.s.pos()
        if not p:
            return None
        px = ref_px * (1 - C.SLIPPAGE_RATE)
        fee = p.qty * px * C.FEE_RATE
        self.s.cash += p.qty * px - fee
        pnl = p.qty * (px - p.entry_px) - p.entry_fee - fee
        trade = {"entry_ts": p.entry_ts, "exit_ts": ts_ms, "entry_px": p.entry_px, "exit_px": px, "qty": p.qty,
                 "pnl": pnl, "ret": pnl / p.entry_equity, "r": pnl / p.init_risk if p.init_risk else None,
                 "reason": reason}
        self.s.trades.append(trade)
        self.s.position = None
        return trade

    def raise_stop(self, level: float) -> bool:
        """El stop sólo puede subir (trailing). Nunca se aleja del precio."""
        if self.s.position and level == level and level > self.s.position["stop"]:
            self.s.position["stop"] = float(level)
            return True
        return False

    def check_stop_candles(self, candles_1m: list[list]) -> dict | None:
        """candles_1m: [[ts, o, h, l, c, v], ...] cerradas, en orden. Ejecuta el stop en la primera que lo toque."""
        p = self.s.pos()
        for ts, o, _h, low, _c, _v in candles_1m:
            if ts < self.s.last_check_ms:
                continue
            if p and low <= p.stop:
                return self.sell(min(o, p.stop), int(ts) + 60_000, "stop")
        if candles_1m:
            self.s.last_check_ms = max(self.s.last_check_ms, int(candles_1m[-1][0]) + 60_000)
        return None

    def check_stop_price(self, last_px: float, ts_ms: int) -> dict | None:
        p = self.s.pos()
        if p and last_px <= p.stop:
            return self.sell(last_px, ts_ms, "stop")
        return None
