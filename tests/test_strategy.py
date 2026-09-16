import time

import pytest

from bot.strategy import (MS_1D, MS_1H, MS_5M, Bar5, H1Bar, StrategyParams, SymbolStrategy,
                          atr_series, build_bars, build_daily_zones, build_hourly, pivots_confirmed)

I0 = 5_000_000  # número de vela de 5m de referencia


def bar(offset, *, high, low, close, prev_close=None, ph5=None, pl5=None, h1=None, new_h1=False,
        d_sup=100.0, d_res=None, d_tol=1.0):
    idx = I0 + offset
    return Bar5(index=idx, time=idx * MS_5M, high=high, low=low, close=close,
                prev_close=prev_close if prev_close is not None else close,
                ph5=ph5, pl5=pl5, h1=h1, new_h1=new_h1, d_sup=d_sup, d_res=d_res, d_tol=d_tol)


def h1(hour, *, high, low, close, last_ph=105.0, last_pl=None, atr=2.0):
    # Horas relativas a la vela de referencia, en la misma escala de tiempo que las de 5m
    return H1Bar(time=I0 * MS_5M + (hour - 1) * MS_1H, high=high, low=low, close=close, last_ph=last_ph, last_pl=last_pl, atr=atr)


def run_long_until_fib(engine):
    """Zona (soporte 100 ± 1) → cambio 1H → extensión → retroceso al 0,618."""
    events = []
    events += engine.on_bar(bar(0, high=102, low=100.6, close=101.8, new_h1=True,
                                h1=h1(1, high=103, low=100.5, close=101.5)))
    events += engine.on_bar(bar(12, high=104, low=100.2, close=103.8, new_h1=True,
                                h1=h1(2, high=104.5, low=100.0, close=104.0)))
    events += engine.on_bar(bar(24, high=106.5, low=103.5, close=106.0, new_h1=True,
                                h1=h1(3, high=107.0, low=103.0, close=106.0)))
    events += engine.on_bar(bar(25, high=108.0, low=106.0, close=107.5))           # extiende el impulso
    events += engine.on_bar(bar(29, high=106.2, low=104.0, close=104.5, ph5=106.0))  # máximo 5m (vela 26)
    events += engine.on_bar(bar(30, high=104.6, low=103.0, close=103.2))           # toca 0,618
    return events


def long_engine():
    p = StrategyParams(allow_short=False)
    return SymbolStrategy("BTC-USDT", p)


def test_long_full_cycle_zone_choch_fib_entry():
    engine = long_engine()
    events = run_long_until_fib(engine)
    assert [e["event"] for e in events] == ["zone", "choch", "fib"]

    zone, choch, fib = events
    assert zone["zone_low"] == 99.0 and zone["zone_high"] == 101.0
    assert zone["id"] == choch["id"] == fib["id"] == f"BTC-USDT-L-{I0 * MS_5M}"
    assert choch["fib_end"] == 107.0 and choch["fib_start"] == 100.0
    # Tras la extensión a 108: impulso 100 → 108
    assert fib["fib_618"] == pytest.approx(108 - 0.618 * 8)
    assert fib["fib_75"] == pytest.approx(102.0)
    assert fib["fib_sl"] == pytest.approx(108 - 0.786 * 8)
    assert engine.long.state == 3

    # Segundo máximo 5m más bajo → diagonal descendente (106 → 105)
    assert engine.on_bar(bar(34, high=105.2, low=103.1, close=103.5, ph5=105.0)) == []
    # Cierre por encima de la diagonal → ENTRADA
    entry = engine.on_bar(bar(35, high=104.8, low=103.4, close=104.5, prev_close=103.5))
    assert len(entry) == 1 and entry[0]["event"] == "entry"
    assert entry[0]["sl"] == pytest.approx(108 - 0.786 * 8)
    assert entry[0]["tp"] == 108.0
    assert entry[0]["time"] == (I0 + 35) * MS_5M + MS_5M
    assert engine.long.state == 0


def test_no_entry_without_breakout():
    engine = long_engine()
    run_long_until_fib(engine)
    engine.on_bar(bar(34, high=105.2, low=103.1, close=103.5, ph5=105.0))
    assert engine.on_bar(bar(35, high=104.0, low=103.4, close=103.9, prev_close=103.5)) == []
    assert engine.long.state == 3


def test_cancel_when_1h_closes_beyond_075():
    engine = long_engine()
    run_long_until_fib(engine)
    ev = engine.on_bar(bar(36, high=103, low=101.0, close=101.5, new_h1=True,
                           h1=h1(4, high=104, low=101.0, close=101.9)))
    assert [e["event"] for e in ev] == ["cancel"] and "0,75" in ev[0]["note"]
    assert engine.long.state == 0


def test_wick_below_075_does_not_cancel_by_default():
    engine = long_engine()
    run_long_until_fib(engine)
    ev = engine.on_bar(bar(36, high=103, low=101.0, close=102.8))  # mecha bajo 0,75 sin cierre 1H
    assert ev == [] and engine.long.state == 3


def test_cancel_when_price_reaches_target_before_trigger():
    engine = long_engine()
    run_long_until_fib(engine)
    ev = engine.on_bar(bar(33, high=108.2, low=104.0, close=107.9))  # vuelve al techo (TP) sin diagonal
    assert [e["event"] for e in ev] == ["cancel"] and "objetivo" in ev[0]["note"]
    assert engine.long.state == 0


def test_cancel_when_zone_lost_before_choch():
    engine = long_engine()
    engine.on_bar(bar(0, high=102, low=100.6, close=101.8, new_h1=True,
                      h1=h1(1, high=103, low=100.5, close=101.5)))
    ev = engine.on_bar(bar(12, high=100, low=97, close=98, new_h1=True,
                           h1=h1(2, high=101, low=97.5, close=98.5)))
    assert [e["event"] for e in ev] == ["cancel"]


def test_choch_expiry():
    engine = long_engine()
    engine.on_bar(bar(0, high=102, low=100.6, close=101.8, new_h1=True,
                      h1=h1(1, high=103, low=100.5, close=101.5)))
    ev = engine.on_bar(bar(700, high=102, low=100.6, close=101.8, new_h1=True,
                           h1=h1(1 + 49, high=103, low=100.5, close=101.5)))
    assert [e["event"] for e in ev] == ["cancel"] and "a tiempo" in ev[0]["note"]


def test_short_mirror_zone_event():
    engine = SymbolStrategy("ETH-USDT", StrategyParams(allow_long=False))
    ev = engine.on_bar(bar(0, high=200.8, low=198, close=199, new_h1=True, d_sup=None, d_res=200.0, d_tol=1.0,
                           h1=h1(1, high=200.5, low=198.0, close=199.5, last_ph=None, last_pl=195.0)))
    assert ev[0]["event"] == "zone" and ev[0]["side"] == "SHORT"
    assert engine.short.struct_lvl == 195.0


# ───────── indicadores y armado de contexto (sin mirar el futuro) ─────────

def candles(values, start=0, step=MS_1H):
    return [{"time": start + i * step, "open": v, "high": v + 1, "low": v - 1, "close": v, "volume": 1}
            for i, v in enumerate(values)]


def test_pivot_confirmed_only_after_right_bars():
    cs = candles([1, 2, 5, 2, 1, 0, 0])
    ph = pivots_confirmed(cs, 2, "high")
    assert ph[:4] == [None] * 4 and ph[4] == 6  # pivote de la vela 2 se conoce en la vela 4


def test_atr_seeds_with_sma():
    cs = candles([10] * 14)
    atr = atr_series(cs)
    assert atr[12] is None and atr[13] == pytest.approx(2.0)


def test_hourly_context_uses_last_closed_hour():
    ctx = build_hourly(candles([100 + i for i in range(10)]), StrategyParams())
    assert ctx.last_closed(3 * MS_1H).time == 2 * MS_1H          # 03:00 → cerrada la de 02:00
    assert ctx.last_closed(3 * MS_1H + 55 * 60_000).time == 2 * MS_1H
    assert ctx.last_closed(4 * MS_1H).time == 3 * MS_1H


def test_daily_zone_detected_from_repeated_lows_and_no_lookahead():
    closes = [115, 112, 110, 105, 100, 105, 110, 112, 108, 104, 100.3, 104, 108, 112, 115, 118, 120, 121,
              119, 117, 115, 113]
    daily = candles(closes, step=MS_1D)
    zones = build_daily_zones(daily, StrategyParams())
    last = len(daily) - 1
    sup, res, tol = zones.at(daily[last]["time"] + MS_1D)   # día siguiente al último cerrado
    assert sup is not None and abs(sup - 99.3) < 1.0          # mínimos 99 y 99,3 agrupados
    # Durante el último día sólo se ven zonas del día anterior
    assert zones.at(daily[last]["time"] + MS_5M) == (zones.sup[last - 1], zones.res[last - 1], zones.tol[last - 1])


def test_build_bars_flags_new_hour():
    hourly = build_hourly(candles([100] * 5), StrategyParams())
    zones = build_daily_zones(candles([100] * 3, step=MS_1D), StrategyParams())
    m5 = candles([100] * 30, start=MS_1H, step=MS_5M)
    bars = build_bars(m5, hourly, zones, StrategyParams())
    flagged = [b.time for b in bars if b.new_h1]
    assert flagged == [2 * MS_1H, 3 * MS_1H]
