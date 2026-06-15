"""
tests/test_metrics.py
Cubre core/metrics.py — la lógica que produce el veredicto cuantitativo, así que
tiene que estar bien.
"""

from datetime import datetime, timedelta

import pytest

from core.metrics import compute_metrics


def _trade(day, pnl):
    base = datetime(2025, 1, 1)
    return {
        "entry_ts": base + timedelta(days=day),
        "exit_ts": base + timedelta(days=day, hours=5),
        "pnl_usdt": pnl,
    }


def test_sin_trades_da_cero():
    m = compute_metrics([], 1000.0)
    assert m["trades"] == 0
    assert m["return_pct"] == 0.0
    assert m["final_capital"] == 1000.0


def test_profit_factor_y_winrate():
    # 3 ganadoras de +20, 2 perdedoras de -10 → PF = 60/20 = 3.0, WR = 60%
    trades = [_trade(0, 20), _trade(1, 20), _trade(2, 20), _trade(3, -10), _trade(4, -10)]
    m = compute_metrics(trades, 1000.0)
    assert m["profit_factor"] == pytest.approx(3.0)
    assert m["win_rate_pct"] == pytest.approx(60.0)
    assert m["expectancy_usdt"] == pytest.approx((60 - 20) / 5)
    assert m["return_pct"] == pytest.approx(4.0)  # +40 / 1000
    assert m["final_capital"] == pytest.approx(1040.0)


def test_max_drawdown():
    # +100 luego -50 → pico 1100, valle 1050 → DD = 50/1100 = 4.55%
    trades = [_trade(0, 100), _trade(1, -50)]
    m = compute_metrics(trades, 1000.0)
    assert m["max_drawdown_pct"] == pytest.approx(50 / 1100 * 100, abs=0.05)


def test_estrategia_perdedora_sharpe_negativo():
    trades = [_trade(i, -10) for i in range(10)]
    m = compute_metrics(trades, 1000.0)
    assert m["return_pct"] < 0
    assert m["profit_factor"] == 0.0  # sin ganancias
    assert m["sharpe"] <= 0


def test_avg_win_loss():
    trades = [_trade(0, 30), _trade(1, -10), _trade(2, -20)]
    m = compute_metrics(trades, 1000.0)
    assert m["avg_win"] == pytest.approx(30.0)
    assert m["avg_loss"] == pytest.approx(-15.0)  # (-10-20)/2
