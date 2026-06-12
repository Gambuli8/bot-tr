"""
tests/test_risk_guard.py
Cubre el kill-switch de drawdown: lógica pura + coordinación de portafolio.
"""

import pytest

from core.risk_guard import (
    drawdown_from_peak,
    breach_reason,
    PortfolioGuard,
)


# ───────── lógica pura ─────────

def test_drawdown_from_peak_basico():
    assert drawdown_from_peak(80, 100) == pytest.approx(0.20)
    assert drawdown_from_peak(100, 100) == 0.0
    assert drawdown_from_peak(110, 100) == 0.0   # equity sobre el pico → 0
    assert drawdown_from_peak(50, 0) == 0.0       # pico inválido → 0


def test_breach_reason_dispara_sobre_limite():
    # 20% de caída con límite 18% → dispara
    assert breach_reason(80, 100, 0.18, "PER-BOT") is not None
    # 10% de caída con límite 18% → no dispara
    assert breach_reason(90, 100, 0.18, "PER-BOT") is None


def test_breach_reason_limite_cero_desactiva():
    # límite 0 = guard apagado, nunca dispara aunque caiga 50%
    assert breach_reason(50, 100, 0.0, "PER-BOT") is None


def test_breach_reason_borde_exacto():
    # caída EXACTA al límite → dispara (>=)
    assert breach_reason(82, 100, 0.18, "PER-BOT") is not None


# ───────── coordinación de portafolio ─────────

def test_portfolio_guard_deshabilitado_sin_dir():
    g = PortfolioGuard("")
    assert g.enabled is False
    assert g.snapshot() is None
    g.publish_equity("BTC", 100, 100)  # no debe explotar


def test_portfolio_agrega_equity_de_varios_bots(tmp_path):
    shared = str(tmp_path / "shared")
    g_btc = PortfolioGuard(shared)
    g_sol = PortfolioGuard(shared)
    g_avax = PortfolioGuard(shared)

    g_btc.publish_equity("BTC", 100, 120)
    g_sol.publish_equity("SOL", 90, 100)
    g_avax.publish_equity("AVAX", 80, 95)

    snap = g_btc.snapshot()
    assert snap is not None
    assert snap.contributors == 3
    assert snap.combined_equity == pytest.approx(270)        # 100+90+80
    assert snap.combined_peak == pytest.approx(270)          # primer pico = equity actual
    assert snap.drawdown == pytest.approx(0.0)


def test_portfolio_drawdown_tras_caida(tmp_path):
    shared = str(tmp_path / "shared")
    g = PortfolioGuard(shared)

    # Pico inicial: 300 combinados
    g.publish_equity("BTC", 100, 100)
    g.publish_equity("SOL", 100, 100)
    g.publish_equity("AVAX", 100, 100)
    snap = g.snapshot()
    assert snap.combined_peak == pytest.approx(300)

    # Crash correlacionado: los 3 caen a 80 → 240 combinados
    g.publish_equity("BTC", 80, 100)
    g.publish_equity("SOL", 80, 100)
    g.publish_equity("AVAX", 80, 100)
    snap = g.snapshot()
    assert snap.combined_equity == pytest.approx(240)
    assert snap.combined_peak == pytest.approx(300)          # el pico persiste
    assert snap.drawdown == pytest.approx(0.20)              # 20% de drawdown
    # Con límite de portafolio 15% → dispararía el HALT
    assert breach_reason(snap.combined_equity, snap.combined_peak, 0.15, "PORT") is not None


def test_portfolio_ignora_equity_stale(tmp_path, monkeypatch):
    shared = str(tmp_path / "shared")
    g = PortfolioGuard(shared)
    g.publish_equity("BTC", 100, 100)
    # Forzamos que el archivo se considere viejo
    monkeypatch.setattr(PortfolioGuard, "STALE_SECONDS", -1)
    assert g.snapshot() is None  # todo stale → sin contributors
