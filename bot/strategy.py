"""
Motor de estrategia interno: Zona 1D + Fibonacci 1H + Diagonal 5m.

Es la traducción 1:1 del indicador `tradingview/bingx_fibo_mtf.pine` para que el
bot analice solo, sin depender de alertas pagas de TradingView. Si se cambia una
regla acá, hay que cambiarla también en el Pine (que queda para verlo en el gráfico).

Cómo se evalúa (igual que el Pine sobre el gráfico de 5m):
  - 1D: sólo días CERRADOS. Zonas = niveles con ≥ N pivotes diarios dentro de ±ancho×ATR.
  - 1H: en cada vela de 5m se mira la última vela de 1H CERRADA (y sus swings/ATR).
  - 5m: el estado avanza vela a vela; los eventos salen al cierre de la vela.

Funciones puras y sin red: el scanner (bot/scanner.py) le pasa las velas.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Optional

MS_5M = 5 * 60_000
MS_1H = 60 * 60_000
MS_1D = 24 * MS_1H


@dataclass
class StrategyParams:
    d_pivot_len: int = 3
    d_max_pivots: int = 40
    d_tol_atr: float = 0.5
    d_min_touches: int = 2
    h_pivot_len: int = 3
    fib_entry: float = 0.618
    fib_inval: float = 0.75
    inval_by_close: bool = True     # True: cierre 1H · False: mecha
    sl_mode: str = "fib"            # fib: nivel sl_fib · atr: 0.75 − colchón×ATR 1H · structure: inicio del impulso − colchón
    sl_fib: float = 0.786
    sl_buf_atr: float = 0.1
    choch_expiry_h: int = 48
    trigger_expiry_h: int = 48
    m_pivot_len: int = 3
    allow_long: bool = True
    allow_short: bool = True
    # Filtros de confluencia (apagados por defecto: se activan de a uno para testear)
    filter_trend: bool = False      # sólo a favor de la EMA diaria (último día cerrado)
    trend_ema_period: int = 50
    filter_impulse: bool = False    # impulso 1H (nivel 1 → 0) ≥ impulse_atr_mult × ATR(14) 1H
    impulse_atr_mult: float = 1.5
    filter_volume: bool = False     # vela gatillo de 5m con volumen > SMA(volume_sma)
    volume_sma: int = 20


# ───────────────────────── indicadores ─────────────────────────

def atr_series(candles: list[dict], period: int = 14) -> list[Optional[float]]:
    """ATR de Wilder (igual a ta.atr de Pine): RMA del true range, semilla = SMA."""
    out: list[Optional[float]] = []
    prev_close = None
    trs: list[float] = []
    rma: Optional[float] = None
    for c in candles:
        tr = c["high"] - c["low"] if prev_close is None else max(
            c["high"] - c["low"], abs(c["high"] - prev_close), abs(c["low"] - prev_close))
        prev_close = c["close"]
        if rma is None:
            trs.append(tr)
            if len(trs) == period:
                rma = sum(trs) / period
        else:
            rma = (rma * (period - 1) + tr) / period
        out.append(rma)
    return out


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    """EMA estilo Pine (ta.ema): semilla = SMA de las primeras `period` velas."""
    out: list[Optional[float]] = []
    k = 2 / (period + 1)
    cur: Optional[float] = None
    for i, v in enumerate(values):
        if cur is None:
            if i + 1 == period:
                cur = sum(values[:period]) / period
        else:
            cur = v * k + cur * (1 - k)
        out.append(cur)
    return out


def sma_series(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        out.append(total / period if i + 1 >= period else None)
    return out


def pivots_confirmed(candles: list[dict], length: int, key: str) -> list[Optional[float]]:
    """
    Valor del pivote CONFIRMADO en cada vela (como ta.pivothigh/pivotlow(len, len)):
    en la vela i devuelve el extremo de la vela i-len si es mayor (menor) que las
    `len` velas a cada lado; si no, None.
    """
    n = len(candles)
    out: list[Optional[float]] = [None] * n
    for i in range(2 * length, n):
        center = i - length
        v = candles[center][key]
        window = [candles[j][key] for j in range(center - length, center + length + 1) if j != center]
        if key == "high" and all(v > w for w in window):
            out[i] = v
        elif key == "low" and all(v < w for w in window):
            out[i] = v
    return out


# ───────────────────────── contexto por temporalidad ─────────────────────────

@dataclass
class DailyZones:
    """Zonas vigentes para un día: calculadas con el último día cerrado."""
    day_open: list[int]                 # open time del día que YA cerró
    sup: list[Optional[float]]
    res: list[Optional[float]]
    tol: list[Optional[float]]
    close: list[float] = field(default_factory=list)
    ema: list[Optional[float]] = field(default_factory=list)

    def _idx(self, t_ms: int) -> int:
        # Último día cerrado antes de la vela t: open_time + 1d <= t
        return bisect.bisect_right(self.day_open, t_ms - MS_1D) - 1

    def at(self, t_ms: int) -> tuple[Optional[float], Optional[float], Optional[float]]:
        idx = self._idx(t_ms)
        if idx < 0:
            return None, None, None
        return self.sup[idx], self.res[idx], self.tol[idx]

    def trend_at(self, t_ms: int) -> tuple[Optional[float], Optional[float]]:
        """(cierre, EMA) del último día cerrado."""
        idx = self._idx(t_ms)
        if idx < 0 or not self.ema:
            return None, None
        return self.close[idx], self.ema[idx]


def build_daily_zones(daily: list[dict], p: StrategyParams) -> DailyZones:
    ph = pivots_confirmed(daily, p.d_pivot_len, "high")
    pl = pivots_confirmed(daily, p.d_pivot_len, "low")
    atr = atr_series(daily)
    piv: list[float] = []
    sups, ress, tols = [], [], []
    for i, c in enumerate(daily):
        if ph[i] is not None:
            piv.append(ph[i])
        if pl[i] is not None:
            piv.append(pl[i])
        del piv[:-p.d_max_pivots]
        tol = atr[i] * p.d_tol_atr if atr[i] is not None else None
        sup = res = None
        if piv and tol is not None:
            for lvl in piv:
                touches = sum(1 for other in piv if abs(other - lvl) <= tol)
                if touches >= p.d_min_touches:
                    if lvl <= c["close"] + tol and (sup is None or lvl > sup):
                        sup = lvl
                    if lvl >= c["close"] - tol and (res is None or lvl < res):
                        res = lvl
        sups.append(sup)
        ress.append(res)
        tols.append(tol)
    closes = [c["close"] for c in daily]
    return DailyZones([c["time"] for c in daily], sups, ress, tols, closes, ema_series(closes, p.trend_ema_period))


@dataclass
class H1Bar:
    time: int
    high: float
    low: float
    close: float
    last_ph: Optional[float]
    last_pl: Optional[float]
    atr: Optional[float]


@dataclass
class HourlyContext:
    open_times: list[int]
    bars: list[H1Bar]

    def last_closed(self, t_ms: int) -> Optional[H1Bar]:
        """Última vela de 1H cerrada al abrir la vela de 5m `t_ms`."""
        idx = bisect.bisect_right(self.open_times, t_ms - MS_1H) - 1
        return self.bars[idx] if idx >= 0 else None


def build_hourly(hourly: list[dict], p: StrategyParams) -> HourlyContext:
    ph = pivots_confirmed(hourly, p.h_pivot_len, "high")
    pl = pivots_confirmed(hourly, p.h_pivot_len, "low")
    atr = atr_series(hourly)
    bars = []
    last_ph = last_pl = None
    for i, c in enumerate(hourly):
        if ph[i] is not None:
            last_ph = ph[i]
        if pl[i] is not None:
            last_pl = pl[i]
        bars.append(H1Bar(c["time"], c["high"], c["low"], c["close"], last_ph, last_pl, atr[i]))
    return HourlyContext([c["time"] for c in hourly], bars)


# ───────────────────────── estado de un setup ─────────────────────────

STAGE_LABELS = {
    1: "en zona diaria, esperando cambio 1H",
    2: "cambio 1H, esperando retroceso al 0,618",
    3: "en 0,618, esperando gatillo 5m",
}


@dataclass
class Setup:
    direction: int                      # 1 LONG · -1 SHORT
    state: int = 0
    id: str = ""
    zone_lo: Optional[float] = None
    zone_hi: Optional[float] = None
    touch_time: Optional[int] = None
    extreme: Optional[float] = None     # nivel 1 (inicio del impulso)
    struct_lvl: Optional[float] = None
    imp_end: Optional[float] = None     # nivel 0 (TP)
    imp_end_bar: Optional[int] = None
    f618: Optional[float] = None
    f75: Optional[float] = None
    fsl: Optional[float] = None
    stage_time: Optional[int] = None
    piv_p: list[float] = field(default_factory=list)
    piv_b: list[int] = field(default_factory=list)
    updated: Optional[int] = None       # ms del último cambio de etapa

    @property
    def side(self) -> str:
        return "LONG" if self.direction == 1 else "SHORT"

    def reset(self) -> None:
        self.__init__(self.direction)   # type: ignore[misc]


@dataclass
class Bar5:
    """Vela de 5m cerrada + contexto que necesita el paso de la estrategia."""
    index: int                          # número de vela absoluto (time // 5m): estable entre reinicios
    time: int                           # open time
    high: float
    low: float
    close: float
    prev_close: Optional[float]
    ph5: Optional[float]
    pl5: Optional[float]
    h1: Optional[H1Bar]
    new_h1: bool
    d_sup: Optional[float]
    d_res: Optional[float]
    d_tol: Optional[float]
    volume: float = 0.0
    vol_sma: Optional[float] = None
    d_close: Optional[float] = None     # cierre del último día cerrado
    d_ema: Optional[float] = None       # EMA diaria del último día cerrado

    @property
    def close_time(self) -> int:
        return self.time + MS_5M


# ───────────────────────── motor ─────────────────────────

class SymbolStrategy:
    """Un LONG y un SHORT en seguimiento por par, como el indicador."""

    def __init__(self, symbol: str, params: Optional[StrategyParams] = None):
        self.symbol = symbol
        self.p = params or StrategyParams()
        self.long = Setup(1)
        self.short = Setup(-1)

    def setups(self) -> list[Setup]:
        out = []
        if self.p.allow_long:
            out.append(self.long)
        if self.p.allow_short:
            out.append(self.short)
        return out

    # ── eventos ──
    def _event(self, s: Setup, event: str, bar: Bar5, price: float, note: str,
               sl: Optional[float] = None, tp: Optional[float] = None) -> dict:
        s.updated = bar.close_time
        ev = {"event": event, "id": s.id, "symbol": self.symbol, "side": s.side, "price": price,
              "time": bar.close_time, "note": note}
        if sl is not None:
            ev["sl"] = sl
        if tp is not None:
            ev["tp"] = tp
        if s.zone_lo is not None:
            ev.update(zone_low=s.zone_lo, zone_high=s.zone_hi)
        if s.imp_end is not None:
            ev.update(fib_start=s.extreme, fib_end=s.imp_end, fib_618=s.f618, fib_75=s.f75, fib_sl=s.fsl)
        return ev

    def _recalc_fib(self, s: Setup) -> None:
        rng = s.imp_end - s.extreme          # positivo en LONG, negativo en SHORT
        s.f618 = s.imp_end - self.p.fib_entry * rng
        s.f75 = s.imp_end - self.p.fib_inval * rng
        s.fsl = s.imp_end - self.p.sl_fib * rng

    def _cancel(self, s: Setup, bar: Bar5, why: str, events: list[dict]) -> None:
        events.append(self._event(s, "cancel", bar, bar.close, why))
        s.reset()

    # ── paso por vela de 5m ──
    def on_bar(self, bar: Bar5) -> list[dict]:
        events: list[dict] = []
        for s in self.setups():
            self._step(s, bar, events)
        return events

    def _step(self, s: Setup, bar: Bar5, events: list[dict]) -> None:
        p = self.p
        is_long = s.direction == 1
        h = bar.h1

        if s.state == 0 and bar.new_h1 and h is not None:
            lvl = bar.d_sup if is_long else bar.d_res
            if lvl is not None and bar.d_tol is not None:
                z_lo, z_hi = lvl - bar.d_tol, lvl + bar.d_tol
                touched = (h.low <= z_hi and h.close >= z_lo) if is_long else (h.high >= z_lo and h.close <= z_hi)
                if touched:
                    s.state = 1
                    s.id = f"{self.symbol}-{'L' if is_long else 'S'}-{h.time}"
                    s.zone_lo, s.zone_hi = z_lo, z_hi
                    s.touch_time = s.stage_time = h.time
                    s.extreme = h.low if is_long else h.high
                    s.struct_lvl = h.last_ph if is_long else h.last_pl
                    events.append(self._event(s, "zone", bar, h.close,
                                              "tocó soporte diario" if is_long else "tocó resistencia diaria"))

        elif s.state == 1 and bar.new_h1 and h is not None:
            s.extreme = min(s.extreme, h.low) if is_long else max(s.extreme, h.high)
            s.struct_lvl = h.last_ph if is_long else h.last_pl
            zone_lost = h.close < s.zone_lo if is_long else h.close > s.zone_hi
            broke = s.struct_lvl is not None and (h.close > s.struct_lvl if is_long else h.close < s.struct_lvl)
            if zone_lost:
                self._cancel(s, bar, "cerró del otro lado de la zona diaria", events)
            elif h.time - s.touch_time > p.choch_expiry_h * MS_1H:
                self._cancel(s, bar, "no hubo cambio de tendencia en 1H a tiempo", events)
            elif broke:
                s.state = 2
                s.stage_time = h.time
                s.imp_end = h.high if is_long else h.low
                s.imp_end_bar = bar.index
                self._recalc_fib(s)
                events.append(self._event(s, "choch", bar, h.close, "rompió el último swing de 1H"))

        elif s.state >= 2:
            if s.state == 2 and (bar.high > s.imp_end if is_long else bar.low < s.imp_end):
                s.imp_end = bar.high if is_long else bar.low
                s.imp_end_bar = bar.index
                s.piv_p.clear()
                s.piv_b.clear()
                self._recalc_fib(s)

            piv_val = bar.ph5 if is_long else bar.pl5
            piv_bar = bar.index - p.m_pivot_len
            if piv_val is not None and piv_bar > s.imp_end_bar:
                s.piv_p.append(piv_val)
                s.piv_b.append(piv_bar)

            if p.inval_by_close:
                broke_inval = bar.new_h1 and h is not None and (h.close < s.f75 if is_long else h.close > s.f75)
            else:
                broke_inval = bar.low < s.f75 if is_long else bar.high > s.f75

            passed_target = s.state == 3 and (bar.high >= s.imp_end if is_long else bar.low <= s.imp_end)
            if broke_inval:
                self._cancel(s, bar, "rompió el 0,75", events)
            elif passed_target:
                self._cancel(s, bar, "el precio volvió al objetivo sin gatillo en 5m", events)
            elif bar.time - s.stage_time > p.trigger_expiry_h * MS_1H:
                self._cancel(s, bar, "no retrocedió al 0,618 a tiempo" if s.state == 2
                             else "no hubo gatillo en 5m a tiempo", events)
            elif s.state == 2 and (bar.low <= s.f618 if is_long else bar.high >= s.f618):
                impulse = abs(s.imp_end - s.extreme)
                if p.filter_impulse and h is not None and h.atr and impulse < p.impulse_atr_mult * h.atr:
                    self._cancel(s, bar, f"impulso débil (menor a {p.impulse_atr_mult:g}×ATR de 1H)", events)
                    return
                s.state = 3
                s.stage_time = bar.time
                events.append(self._event(s, "fib", bar, bar.close, "retroceso en 0,618 sin romper 0,75"))
            elif s.state == 3 and s.piv_p:
                n = len(s.piv_p)
                y1 = s.piv_p[n - 2] if n >= 2 else s.imp_end
                x1 = s.piv_b[n - 2] if n >= 2 else s.imp_end_bar
                y2, x2 = s.piv_p[n - 1], s.piv_b[n - 1]
                converging = y2 < y1 if is_long else y2 > y1
                if converging and x2 > x1 and bar.prev_close is not None:
                    slope = (y2 - y1) / (x2 - x1)
                    line_now = y2 + slope * (bar.index - x2)
                    line_prev = line_now - slope
                    breakout = (bar.close > line_now and bar.prev_close <= line_prev) if is_long \
                        else (bar.close < line_now and bar.prev_close >= line_prev)
                    inside = bar.close > s.f75 if is_long else bar.close < s.f75
                    if breakout and inside and p.filter_volume and not (bar.vol_sma and bar.volume > bar.vol_sma):
                        return  # ruptura sin volumen: se sigue esperando otra
                    if breakout and inside and p.filter_trend:
                        if bar.d_ema is None or bar.d_close is None or \
                                (bar.d_close <= bar.d_ema if is_long else bar.d_close >= bar.d_ema):
                            self._cancel(s, bar, f"va contra la tendencia diaria (EMA{p.trend_ema_period})", events)
                            return
                    if breakout and inside:
                        buf = p.sl_buf_atr * (h.atr or 0) if h else 0
                        if p.sl_mode == "structure":
                            sl = s.extreme - buf if is_long else s.extreme + buf
                        elif p.sl_mode == "atr":
                            sl = s.f75 - buf if is_long else s.f75 + buf
                        else:
                            sl = s.fsl
                        events.append(self._event(s, "entry", bar, bar.close,
                                                  "ruptura de la diagonal en 5m", sl=sl, tp=s.imp_end))
                        s.reset()


# ───────────────────────── armado de velas ─────────────────────────

def build_bars(m5: list[dict], hourly: HourlyContext, zones: DailyZones, p: StrategyParams) -> list[Bar5]:
    """Convierte velas de 5m cerradas en Bar5 con todo el contexto (sin mirar el futuro)."""
    ph5 = pivots_confirmed(m5, p.m_pivot_len, "high")
    pl5 = pivots_confirmed(m5, p.m_pivot_len, "low")
    vol_sma = sma_series([c.get("volume", 0.0) for c in m5], p.volume_sma)
    bars: list[Bar5] = []
    prev_h1_time: Optional[int] = None
    for i, c in enumerate(m5):
        h1 = hourly.last_closed(c["time"])
        d_sup, d_res, d_tol = zones.at(c["time"])
        d_close, d_ema = zones.trend_at(c["time"])
        h1_time = h1.time if h1 else None
        bars.append(Bar5(
            index=c["time"] // MS_5M, time=c["time"], high=c["high"], low=c["low"], close=c["close"],
            prev_close=m5[i - 1]["close"] if i > 0 else None,
            ph5=ph5[i], pl5=pl5[i], h1=h1,
            new_h1=h1_time is not None and h1_time != prev_h1_time and i > 0,
            d_sup=d_sup, d_res=d_res, d_tol=d_tol,
            volume=c.get("volume", 0.0), vol_sma=vol_sma[i], d_close=d_close, d_ema=d_ema,
        ))
        prev_h1_time = h1_time
    return bars
