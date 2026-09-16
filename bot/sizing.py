"""
Cálculo del tamaño de la operación con MARGEN FIJO (1–2 USDT).

Regla: el margen es fijo; el apalancamiento es el MÍNIMO necesario para que la
orden cumpla los mínimos de BingX (cantidad mínima y notional mínimo).

Controles que rechazan el trade (mejor no entrar que entrar mal):
  - SL/TP del lado equivocado respecto al precio.
  - Apalancamiento necesario > MAX_LEVERAGE.
  - La liquidación quedaría ANTES que el stop loss (el SL nunca se ejecutaría).
  - R:R neto de comisiones < MIN_RR.

Funciones puras: sin red, 100% testeables.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Optional

from bot.bingx import ContractSpec

# Margen de mantenimiento aproximado para estimar liquidación (isolated).
DEFAULT_MMR = 0.005
# El SL tiene que quedar a menos de este % de la distancia a liquidación.
LIQ_SAFETY = 0.8


@dataclass
class TradePlan:
    ok: bool
    reason: str
    symbol: str = ""
    direction: str = ""
    entry: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    leverage: int = 0
    qty: float = 0.0
    qty_str: str = ""
    sl_str: str = ""
    tp_str: str = ""
    notional: float = 0.0
    margin_used: float = 0.0
    sl_distance_pct: float = 0.0
    tp_distance_pct: float = 0.0
    risk_usdt: float = 0.0     # pérdida si toca SL (incluye comisiones)
    reward_usdt: float = 0.0   # ganancia si toca TP (neta de comisiones)
    fees_usdt: float = 0.0
    rr: float = 0.0
    liquidation_price: float = 0.0


def round_step(value: float, precision: int, mode=ROUND_DOWN) -> Decimal:
    quantum = Decimal(1).scaleb(-precision)
    return Decimal(str(value)).quantize(quantum, rounding=mode)


def estimate_liquidation(entry: float, leverage: float, direction: str, mmr: float = DEFAULT_MMR) -> float:
    if direction == "SHORT":
        return entry * (1 + 1.0 / leverage - mmr)
    return entry * (1 - 1.0 / leverage + mmr)


def build_plan(
    *,
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    spec: ContractSpec,
    margin_usdt: float,
    max_leverage: int,
    min_rr: float,
    mmr: float = DEFAULT_MMR,
) -> TradePlan:
    base = TradePlan(ok=False, reason="", symbol=symbol, direction=direction,
                     entry=entry, stop_loss=stop_loss, take_profit=take_profit)

    if direction not in ("LONG", "SHORT"):
        base.reason = f"dirección inválida: {direction}"
        return base
    if min(entry, stop_loss, take_profit) <= 0:
        base.reason = "precios inválidos (<= 0)"
        return base
    if direction == "LONG" and not (stop_loss < entry < take_profit):
        base.reason = f"LONG requiere SL < precio < TP (SL {stop_loss}, precio {entry}, TP {take_profit})"
        return base
    if direction == "SHORT" and not (take_profit < entry < stop_loss):
        base.reason = f"SHORT requiere TP < precio < SL (TP {take_profit}, precio {entry}, SL {stop_loss})"
        return base

    sl_dist = abs(entry - stop_loss) / entry
    tp_dist = abs(take_profit - entry) / entry
    min_qty = Decimal(str(spec.min_qty))

    # Apalancamiento mínimo que cumple cantidad mínima y notional mínimo.
    leverage = max(1, math.ceil(max(spec.min_qty * entry, spec.min_usdt) / margin_usdt))
    qty = Decimal(0)
    while leverage <= max_leverage:
        qty = round_step(margin_usdt * leverage / entry, spec.qty_precision)
        if qty >= min_qty and float(qty) * entry >= spec.min_usdt:
            break
        leverage += 1
    if leverage > max_leverage:
        base.reason = (f"con {margin_usdt} USDT de margen haría falta más de {max_leverage}x "
                       f"para el mínimo de {spec.symbol} ({spec.min_qty} ≈ {spec.min_qty * entry:.2f} USDT)")
        return base

    liq = estimate_liquidation(entry, leverage, direction, mmr)
    liq_dist = abs(entry - liq) / entry
    if sl_dist >= liq_dist * LIQ_SAFETY:
        base.reason = (f"el SL está a {sl_dist:.2%} pero con {leverage}x la liquidación está a {liq_dist:.2%}: "
                       f"liquidaría antes de tocar el stop")
        return base

    qty_f = float(qty)
    notional = qty_f * entry
    fees = notional * spec.taker_fee * 2
    risk = qty_f * abs(entry - stop_loss) + fees
    reward = qty_f * abs(take_profit - entry) - fees
    rr = reward / risk if risk > 0 else 0.0

    plan = TradePlan(
        ok=True, reason="ok", symbol=symbol, direction=direction,
        entry=entry, stop_loss=stop_loss, take_profit=take_profit,
        leverage=leverage, qty=qty_f, qty_str=format(qty, "f"),
        sl_str=format(round_step(stop_loss, spec.price_precision, ROUND_HALF_UP), "f"),
        tp_str=format(round_step(take_profit, spec.price_precision, ROUND_HALF_UP), "f"),
        notional=notional, margin_used=notional / leverage,
        sl_distance_pct=sl_dist * 100, tp_distance_pct=tp_dist * 100,
        risk_usdt=risk, reward_usdt=reward, fees_usdt=fees, rr=rr, liquidation_price=liq,
    )
    if rr < min_rr:
        plan.ok = False
        plan.reason = f"R:R neto {rr:.2f} menor al mínimo {min_rr}"
    return plan


def pnl_pct_of_margin(pnl: float, margin: Optional[float]) -> float:
    return (pnl / margin * 100) if margin else 0.0
