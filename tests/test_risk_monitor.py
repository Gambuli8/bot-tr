"""
tests/test_risk_monitor.py
Cubre las funciones puras de core/risk_monitor.py.
"""

import pytest

from core.risk_monitor import (
    estimate_liquidation_price,
    liquidation_distance_pct,
    evaluate,
)


# ───────── liquidación ─────────

def test_liq_long_por_debajo_del_entry():
    liq = estimate_liquidation_price(100.0, 7, "LONG")
    # ~100*(1 - 1/7 + 0.005) ≈ 86.2
    assert 85 < liq < 88


def test_liq_short_por_encima_del_entry():
    liq = estimate_liquidation_price(100.0, 7, "SHORT")
    assert 112 < liq < 115


def test_liq_invalida():
    assert estimate_liquidation_price(0, 7, "LONG") == 0.0
    assert estimate_liquidation_price(100, 0, "LONG") == 0.0


def test_distancia_long():
    # precio 100, liq 90 → 10% de margen
    assert liquidation_distance_pct(100.0, 90.0, "LONG") == pytest.approx(10.0)


def test_distancia_short():
    assert liquidation_distance_pct(100.0, 110.0, "SHORT") == pytest.approx(10.0)


# ───────── evaluate ─────────

def _pos(direction="LONG", entry=100.0):
    return {"direction": direction, "entry_price": entry}


def test_no_alerta_cuando_todo_seguro():
    # precio = entry, lejos de liq, sin drawdown
    alerts = evaluate([_pos()], price=100.0, leverage=7, daily_dd=0.0, daily_limit=0.05)
    assert alerts == []


def test_alerta_liquidacion_cerca():
    # LONG entry 100, liq ~86.2. Precio 88 → distancia ~2% < 3% → alerta.
    alerts = evaluate([_pos()], price=88.0, leverage=7, daily_dd=0.0, daily_limit=0.05)
    keys = {a.key for a in alerts}
    assert "liq:LONG" in keys
    assert any(a.severity == "critical" for a in alerts)


def test_alerta_drawdown_diario():
    # daily_dd 4.5% con límite 5% y ratio 0.8 → 4.5% >= 4% → alerta.
    alerts = evaluate([], price=100.0, leverage=7, daily_dd=0.045, daily_limit=0.05)
    keys = {a.key for a in alerts}
    assert "daily_dd" in keys


def test_drawdown_por_debajo_del_umbral_no_alerta():
    alerts = evaluate([], price=100.0, leverage=7, daily_dd=0.03, daily_limit=0.05)
    assert all(a.key != "daily_dd" for a in alerts)


def test_short_liquidacion_cerca():
    # SHORT entry 100, liq ~113.6. Precio 111 → distancia ~2.3% < 3% → alerta.
    alerts = evaluate([_pos("SHORT")], price=111.0, leverage=7, daily_dd=0.0, daily_limit=0.05)
    assert "liq:SHORT" in {a.key for a in alerts}
