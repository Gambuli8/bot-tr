"""
core/risk_monitor.py
Alertas de riesgo proactivas. Funciones puras → testeables sin red.
El disparo y el anti-spam (dedup por cruce de umbral) lo maneja la strategy.

Dos alertas:
  1. Cercanía a la liquidación de una posición abierta (backstop: el SL debería
     dispararse mucho antes, pero si el SL se cancela o hay un gap, esto avisa).
  2. Drawdown del día acercándose al límite que frena el bot (accionable: podés
     pausar o revisar antes de que frene solo).
"""

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class RiskAlert:
    key: str                              # id estable para dedup (ej. "liq:LONG")
    severity: Literal["warning", "critical"]
    message: str


def estimate_liquidation_price(
    entry: float, leverage: float, direction: str, mmr: float = 0.005
) -> float:
    """
    Estimación conservadora del precio de liquidación en margin isolated.
    LONG : liq = entry * (1 - 1/lev + mmr)
    SHORT: liq = entry * (1 + 1/lev - mmr)
    mmr = maintenance margin rate aproximado (~0.4-0.5% en pares líquidos).
    """
    if entry <= 0 or leverage <= 0:
        return 0.0
    if direction == "SHORT":
        return entry * (1 + 1.0 / leverage - mmr)
    return entry * (1 - 1.0 / leverage + mmr)


def liquidation_distance_pct(
    price: float, liq_price: float, direction: str
) -> Optional[float]:
    """% que falta desde el precio actual para tocar la liquidación (>=0 si seguro)."""
    if price <= 0 or liq_price <= 0:
        return None
    if direction == "SHORT":
        return (liq_price - price) / price * 100.0   # liq está arriba
    return (price - liq_price) / price * 100.0        # liq está abajo


def evaluate(
    positions: list,
    price: float,
    leverage: float,
    daily_dd: float,
    daily_limit: float,
    liq_alert_pct: float = 3.0,
    dd_warn_ratio: float = 0.8,
) -> list[RiskAlert]:
    """
    Devuelve la lista de alertas ACTIVAS en este instante.
    - positions: lista de dicts con entry_price y direction.
    - price: precio actual.
    - daily_dd, daily_limit: fracciones (0.04 = 4%).
    - liq_alert_pct: dispara si la distancia a la liquidación <= este % .
    - dd_warn_ratio: dispara si daily_dd >= ratio * daily_limit.
    """
    alerts: list[RiskAlert] = []

    for p in positions or []:
        direction = p.get("direction", "LONG")
        entry = float(p.get("entry_price", 0) or 0)
        liq = estimate_liquidation_price(entry, leverage, direction)
        dist = liquidation_distance_pct(price, liq, direction)
        if dist is not None and dist <= liq_alert_pct:
            alerts.append(RiskAlert(
                key=f"liq:{direction}",
                severity="critical",
                message=(
                    f"Precio ${price:,.2f} a {dist:.1f}% de la liquidación "
                    f"estimada (${liq:,.2f}) — posición {direction} {leverage:.0f}x. "
                    f"Revisá que el stop-loss siga puesto."
                ),
            ))

    if daily_limit > 0 and daily_dd >= dd_warn_ratio * daily_limit:
        alerts.append(RiskAlert(
            key="daily_dd",
            severity="warning",
            message=(
                f"Drawdown del día {daily_dd * 100:.1f}% — cerca del freno "
                f"automático ({daily_limit * 100:.0f}%). El bot se detiene si lo toca."
            ),
        ))

    return alerts
