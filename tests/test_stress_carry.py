import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stress_carry import (  # noqa: E402
    STEPS_D, STEPS_H, Path_, scenarios, simulate, worst_funding, worst_rise, worst_wick,
)


def test_flat_market_costs_only_fees():
    r = simulate(Path_(funding=0.0).flat(2 * STEPS_D).bars, leverage=2)
    assert r.liquidations == 0
    assert -0.005 * r.capital < r.pnl < 0          # entrada + salida ≈ 0,25 %


def test_crash_is_hedged():
    r = simulate(Path_().flat(STEPS_H).ramp(50, 7 * STEPS_D).flat(STEPS_D).bars, leverage=2)
    assert r.liquidations == 0
    assert r.pnl > -0.01 * r.capital


def test_rally_with_bot_up_is_protected():
    r = simulate(Path_().flat(STEPS_H).ramp(200, 3 * STEPS_D).bars, leverage=2)
    assert r.liquidations == 0
    assert r.protections >= 3


def test_wick_below_liquidation_is_harmless_and_above_liquidates():
    safe = simulate(Path_().flat(STEPS_H).wick(0.40).flat(STEPS_H).bars, leverage=2)
    hit = simulate(Path_().flat(STEPS_H).wick(0.60).flat(STEPS_H).bars, leverage=2)
    assert safe.liquidations == 0
    assert hit.liquidations == 1
    assert hit.pnl < -0.30 * hit.capital            # se pierde el margen (1/3 del capital con ×2)


def test_liq_guard_covers_band_edge_wick():
    path = Path_().flat(STEPS_H).ramp(114.85, 6 * STEPS_H).wick(0.30).ramp(100, 6 * STEPS_H).bars
    assert simulate(path, leverage=2, liq_guard=0.5).liquidations == 1
    assert simulate(path, leverage=2, liq_guard=0.7).liquidations == 0


def test_bot_down_rally_then_retrace_hurts_more_with_leverage():
    path = (Path_().flat(STEPS_H).ramp(150, 3 * STEPS_H, bot_up=False)
            .ramp(117, 3 * STEPS_H, bot_up=False).flat(STEPS_D).bars)
    x1 = simulate(path, leverage=1)
    x2 = simulate(path, leverage=2)
    assert x1.liquidations == 0 and x2.liquidations == 1
    assert x2.pnl < x1.pnl - 0.15 * x2.capital


def test_negative_funding_cost_matches_formula():
    days, rate = 7, -0.001
    r = simulate(Path_().flat(days * STEPS_D + 1, funding=rate).bars, leverage=2)
    expected = rate * 3 * days * r.capital * 2 / 3
    assert r.funding == pytest.approx(expected, rel=0.02)


def test_spread_multiplier_raises_costs():
    bars = Path_().flat(STEPS_H).bars
    assert simulate(bars, 2, spread_mult=3).pnl < simulate(bars, 2, spread_mult=1).pnl


def test_all_scenarios_run():
    for lev in (1, 2, 3):
        for sc in scenarios(lev):
            r = simulate(sc.bars, lev, spread_mult=sc.spread_mult)
            assert r.final > 0


def test_historical_stats():
    candles = [{"close": 100, "high": 101, "low": 99}, {"close": 100, "high": 130, "low": 98},
               {"close": 110, "high": 112, "low": 90}, {"close": 120, "high": 125, "low": 115}]
    assert worst_wick(candles) == pytest.approx(0.30)
    assert worst_rise(candles, 2) == pytest.approx(125 / 90 - 1)
    total, avg = worst_funding([0.001, -0.002, -0.003, 0.001], 2)
    assert total == pytest.approx(-0.005)
    assert avg == pytest.approx(-0.0025)
