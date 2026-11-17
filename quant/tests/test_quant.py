"""Tests del pipeline cuantitativo: lo que no puede fallar nunca (riesgo, costos, no mirar al futuro)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant import backtest as bt
from quant import config as C
from quant.broker import PaperBroker, PaperState, load_state, save_state
from quant.data import repair_gaps
from quant.risk import RiskManager, RiskState, position_size
from quant.strategies import STRATEGIES, signals


def synthetic_df(n=1500, seed=0, drift=0.0003):
    rng = np.random.default_rng(seed)
    close = 20000 * np.exp(np.cumsum(rng.normal(drift, 0.012, n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.006, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.006, n))
    ts = np.arange(n, dtype=np.int64) * C.BAR_MS + 1_600_000_000_000 // C.BAR_MS * C.BAR_MS
    df = pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low, "close": close,
                       "volume": 1.0, "synthetic": 0})
    df["dt"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    return df


# ── Riesgo ──────────────────────────────────────────────────────────────────────────────────────────────
def test_position_size_risks_at_most_one_percent_including_costs():
    d = position_size(1000, 60000, 58200)  # stop a 3 %
    assert d.ok
    worst = d.qty * (60000 - 58200 * (1 - C.SLIPPAGE_RATE)) + d.qty * 60000 * C.FEE_RATE + d.qty * 58200 * C.FEE_RATE
    assert worst <= 1000 * C.RISK_PER_TRADE * 1.001


def test_position_size_never_leverages():
    d = position_size(1000, 60000, 59700)  # stop a 0,5 % → por riesgo pediría 2x
    assert d.qty * 60000 <= 1000 * C.MAX_POSITION_PCT


@pytest.mark.parametrize("stop,reason", [(60100, "stop_above_entry"), (59950, "stop_too_tight"),
                                         (40000, "stop_too_wide")])
def test_position_size_rejects_bad_stops(stop, reason):
    assert position_size(1000, 60000, stop).reason == reason


def test_min_notional():
    assert position_size(10, 60000, 54000).reason == "below_min_notional"


def test_daily_kill_switch_and_reset_next_day():
    r = RiskManager(RiskState(peak_equity=1000, day_start_equity=1000))
    r.on_new_day("2026-01-01", 1000)
    assert r.check(975) is None
    assert r.check(969.9) == "flatten_day"
    assert r.can_enter() == (False, "daily_loss_lock")
    assert r.check(960) is None  # no re-dispara
    r.on_new_day("2026-01-02", 960)
    assert r.can_enter() == (True, "ok")


def test_drawdown_halt_is_permanent():
    r = RiskManager(RiskState(peak_equity=1000, day_start_equity=1000))
    r.on_new_day("2026-01-01", 1000)
    r.check(1200)
    r.on_new_day("2026-01-02", 1200)
    r.on_new_day("2026-01-03", 1030)
    assert r.check(1019) == "halt"  # −15,08 % desde 1200
    r.on_new_day("2026-01-04", 1019)
    assert r.can_enter() == (False, "halted_max_drawdown")


# ── Datos ───────────────────────────────────────────────────────────────────────────────────────────────
def test_repair_gaps_marks_synthetic_bars():
    df = synthetic_df(10).drop(index=[3, 4])[["ts", "open", "high", "low", "close", "volume"]]
    out, n = repair_gaps(df)
    assert n == 2 and len(out) == 10
    assert out.loc[3, "synthetic"] == 1 and out.loc[3, "volume"] == 0
    assert out.loc[3, "close"] == out.loc[2, "close"] == out.loc[3, "high"]


# ── Estrategias: sin mirar al futuro ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", list(STRATEGIES))
def test_signals_do_not_look_ahead(name):
    df = synthetic_df(800)
    p = STRATEGIES[name].default
    full = signals(name, df, p)
    for cut in (400, 550, 799):
        part = signals(name, df.iloc[:cut + 1].copy(), p)
        pd.testing.assert_frame_equal(part.iloc[-1:], full.iloc[cut:cut + 1], check_exact=False, rtol=1e-9)


def test_synthetic_bars_never_trigger_entries():
    df = synthetic_df(600)
    df.loc[300:310, "synthetic"] = 1
    sig = signals("rsi_reversion", df, {"n": 2, "entry_lvl": 99, "exit_lvl": 100, "trend": 200, "atr_mult": 3})
    assert not sig.loc[300:310, "entry"].any()


# ── Backtest: costos y ejecución ────────────────────────────────────────────────────────────────────────
def test_costs_reduce_profit_and_zero_costs_bound_it():
    df = synthetic_df(2000, drift=0.0008)
    sig = signals("breakout", df, STRATEGIES["breakout"].default)
    base = bt.run(df, sig).stats
    free = bt.run(df, sig, bt.Costs(0, 0, 0)).stats
    x2 = bt.run(df, sig, bt.Costs().scaled(2)).stats
    assert free["net_return"] > base["net_return"] > x2["net_return"]
    assert base["fees"] > 0


def test_funding_is_charged_when_nonzero():
    df = synthetic_df(1500, drift=0.0008)
    sig = signals("ema_cross", df, STRATEGIES["ema_cross"].default)
    r = bt.run(df, sig, bt.Costs(funding_8h=0.0001))
    assert r.stats["funding"] > 0


def test_gap_through_stop_fills_at_open_not_stop():
    df = synthetic_df(30, drift=0)
    df[["open", "high", "low", "close"]] = 100.0
    df.loc[11:, ["open", "high", "low", "close"]] = 90.0  # gap de −10 % en la vela 11
    sig = pd.DataFrame({"entry": False, "exit": False, "stop_dist": 3.0, "trail": np.nan}, index=df.index)
    sig.loc[9, "entry"] = True
    r = bt.run(df, sig)
    t = r.trades.iloc[0]
    assert t.reason == "stop"
    assert t.exit_px == pytest.approx(90 * (1 - C.SLIPPAGE_RATE))
    assert t.pnl < 0 and t.ret < -0.01  # un gap puede superar el 1 %: así se ve el riesgo real


# ── Paper broker ────────────────────────────────────────────────────────────────────────────────────────
def test_broker_stop_from_minute_candles_and_persistence(tmp_path):
    b = PaperBroker(PaperState())
    ok, _ = b.buy(60000, 1800, ts_ms=1_000)
    assert ok
    assert b.raise_stop(58500) and not b.raise_stop(57000)  # el stop sólo sube
    candles = [[60_000, 59000, 59100, 58900, 59000, 1], [120_000, 58600, 58700, 58000, 58100, 1]]
    t = b.check_stop_candles(candles)
    assert t and t["reason"] == "stop"
    assert t["exit_px"] == pytest.approx(58500 * (1 - C.SLIPPAGE_RATE))
    assert b.s.position is None and b.s.cash < C.INITIAL_CAPITAL_USDT
    p = tmp_path / "s.json"
    save_state(b.s, p)
    assert load_state(p).trades == b.s.trades


# ── Bucle del bot con exchange falso ────────────────────────────────────────────────────────────────────
class FakeEx:
    def __init__(self, df, last):
        self.df, self.last, self.m1 = df, last, []

    def parse_timeframe(self, tf):
        return {"1m": 60, "4h": 4 * 3600}[tf]

    def fetch_ohlcv(self, symbol, tf, since=None, limit=1000):
        rows = self.m1 if tf == "1m" else self.df[["ts", "open", "high", "low", "close", "volume"]].values.tolist()
        return [r for r in rows if r[0] >= since][:limit]

    def fetch_ticker(self, symbol):
        return {"last": self.last}


def test_bot_step_entry_then_stop_after_outage_then_daily_kill(monkeypatch):
    import time as _t
    from quant import bot

    now_ms = int(_t.time() * 1000)
    last_open = bot.last_closed_bar_open(now_ms)
    df = synthetic_df(450, drift=0)
    df["ts"] = last_open - (len(df) - 1 - np.arange(len(df))) * C.BAR_MS
    fake_sig = pd.DataFrame({"entry": True, "exit": False, "stop_dist": 1500.0, "trail": np.nan}, index=df.index)
    monkeypatch.setattr(bot, "signals", lambda *a, **k: fake_sig)
    monkeypatch.setattr(bot, "BAR_CLOSE_GRACE_MS", -C.BAR_MS)  # no esperar el cierre real
    ex = FakeEx(df, last=50000.0)
    state = PaperState()
    b, r = PaperBroker(state), RiskManager(RiskState())

    bot.step(ex, b, r, "breakout", {})
    assert state.position and state.position["stop"] == pytest.approx(50000 * (1 + C.SLIPPAGE_RATE) - 1500)

    # caída de red: al volver, una vela de 1m del período caído tocó el stop → se ejecuta ahí, no al precio actual
    state.last_check_ms = state.last_check_ms - 30 * 60_000  # 30 min sin conexión
    t0 = state.last_check_ms
    ex.m1 = [[t0 + 60_000, 49500, 49600, 48000, 48100, 1]]  # vela ya cerrada dentro del corte
    ex.last = 49000.0
    state.last_bar_ts = last_open  # sin vela nueva
    bot.step(ex, b, r, "breakout", {})
    assert state.position is None and state.trades[-1]["reason"] == "stop"
    assert state.trades[-1]["exit_px"] == pytest.approx(state.trades[-1]["entry_px"] - 1500, rel=1e-3)

    # kill-switch diario: el equity cae >3 % en el día → bloqueo de entradas
    state.cash = r.s.day_start_equity * 0.96
    bot.step(ex, b, r, "breakout", {})
    assert r.s.daily_locked and r.can_enter() == (False, "daily_loss_lock")
