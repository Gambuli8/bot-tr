"""Tests de la investigación NQ/Apex con datos sintéticos (no necesitan Databento)."""

import random
import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research" / "nq"))

import apex_sim  # noqa: E402
import nq_backtest as bt  # noqa: E402
import nq_data  # noqa: E402


def synthetic(days: int = 40, seed: int = 3, roll_day: int | None = 20) -> pd.DataFrame:
    """Velas de 1 min de lunes a viernes, 18:00→17:00 ET, con un vencimiento a mitad de camino."""
    rng = random.Random(seed)
    rows, px, inst = [], 15000.0, 1
    start = pd.Timestamp("2024-03-03 18:00", tz="America/New_York")   # domingo
    for d in range(days):
        open_ = start + pd.Timedelta(days=d)
        if open_.weekday() in (4, 5):        # viernes y sábado a la noche no hay sesión
            continue
        if roll_day is not None and d == roll_day:
            inst, px = 2, px + 150.0         # el contrato nuevo arranca 150 puntos más arriba
        for m in range(23 * 60):
            ts = open_ + pd.Timedelta(minutes=m)
            o = px
            px += rng.gauss(0, 3)
            hi, lo = max(o, px) + abs(rng.gauss(0, 1)), min(o, px) - abs(rng.gauss(0, 1))
            rows.append((int(ts.tz_convert("UTC").timestamp() * 1000), inst, o, hi, lo, px, 10))
    return pd.DataFrame(rows, columns=["ts", "instrument_id", "open", "high", "low", "close", "volume"])


@pytest.fixture(scope="module")
def nq():
    return nq_data.prepare(synthetic())


def test_back_adjust_removes_roll_gap():
    raw = synthetic(days=10, roll_day=2)
    adj = nq_data.back_adjust(raw)
    i = raw.index[raw["instrument_id"].ne(raw["instrument_id"].shift())][1]
    assert raw.loc[i, "open"] - raw.loc[i - 1, "close"] == pytest.approx(150.0)
    assert adj.loc[i, "open"] - adj.loc[i - 1, "close"] == pytest.approx(0.0)
    assert adj["close"].iloc[-1] == raw["close"].iloc[-1]          # lo reciente queda con precio real


def test_sessions_and_rth(nq):
    rth = nq_data.rth_bounds(nq)
    lo, hi = next(iter(rth.values()))
    assert nq.minute[lo] == 9 * 60 + 30 and nq.minute[hi] == 15 * 60 + 59
    assert hi - lo + 1 == 390
    assert all(d.weekday() < 5 for d in rth)


def test_orb_rules(nq):
    rth = nq_data.rth_bounds(nq)
    daily = nq_data.daily_sessions(nq)
    trades = bt.orb(nq, rth, daily, "ORB")
    assert trades
    for t in trades:
        lo, hi = rth[t.session]
        i0 = lo
        first_up = nq.c[i0 + 4] > nq.o[i0]
        assert (t.side == "LONG") == first_up
        assert t.entry == nq.o[i0 + 5]
        assert t.stop == (nq.l[i0:i0 + 5].min() if t.side == "LONG" else nq.h[i0:i0 + 5].max())
        assert t.reason in ("SL", "TIEMPO")
        if t.reason == "SL":
            assert t.pnl_pts <= 0


def test_target_variant_and_sizing(nq):
    rth = nq_data.rth_bounds(nq)
    trades = bt.orb(nq, rth, nq_data.daily_sessions(nq), "ORB-2R")
    for t in trades:
        if t.reason == "TP":
            assert t.pnl_pts == pytest.approx(2 * t.stop_pts)
    k = bt.Costs(commission=1.5, slip_ticks=1, max_micros=60)
    t = bt.Trade("X", pd.Timestamp("2024-01-02"), "LONG", 0, 100.0, 90.0, None, 0, 80.0, "SL")
    n, pnl = bt.size_pnl(t, 375, k)
    assert n == int(375 // (10 * 2 + 1.5 + 1.0))
    assert pnl == pytest.approx(n * (-20 * 2 - 1.5 - 1.0))
    assert -pnl / 375 > 1                      # un hueco más allá del stop pierde más de 1R


def test_momentum_and_alex_run(nq):
    rth = nq_data.rth_bounds(nq)
    daily = nq_data.daily_sessions(nq)
    for t in bt.momentum(nq, rth, daily):
        assert nq.minute[nq.at_or_after(t.entry_ts)] == 15 * 60 + 30
    alex = bt.alex(nq)                          # puede no haber setups en 40 días sintéticos
    assert len({t.session for t in alex}) == len(alex)


def test_apex_eval_rules():
    r = apex_sim.Rules()
    dates = list(pd.bdate_range("2025-01-01", periods=40))
    assert apex_sim.run_eval([1000.0] * 5, dates[:5], r) == ("pass", 3)
    assert apex_sim.run_eval([-700.0] * 5, dates[:5], r)[0] == "fail"
    # EOD trailing: sube a +1.500, el umbral pasa a −500 y volver a −500 elimina
    assert apex_sim.run_eval([1500.0, -1000.0, -1000.0], dates[:3], r) == ("fail", 3)
    # pérdida diaria máxima: el día se corta en −1.000
    assert apex_sim.run_eval([-5000.0, 0, 0], dates[:3], r)[0] != "fail"
    assert apex_sim.run_eval([10.0] * 40, dates, r)[0] == "expired"


def test_apex_pa_rules():
    r = apex_sim.Rules()
    assert apex_sim.run_pa([600.0] * 5, r)[0] == "payout"
    status, days, amount = apex_sim.run_pa([300.0] * 9, r)
    assert (status, days) == ("payout", 9) and amount == pytest.approx(600.0)   # 52.700 − 52.100
    assert apex_sim.run_pa([2700.0, 100, 100, 100, 100], r)[0] != "payout"   # consistencia 50 %
    assert apex_sim.run_pa([-700.0] * 3, r)[0] == "fail"
    # el umbral se congela en +100: después de +2.100 se puede bajar hasta +101 sin quemar
    assert apex_sim.run_pa([2100.0, -1990.0] + [0.0] * 3, r)[0] == "unresolved"
