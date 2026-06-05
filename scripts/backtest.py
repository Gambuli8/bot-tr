"""
scripts/backtest.py
Backtest del TechnicalEngine sobre OHLCV histórico.

Uso:
    python scripts/backtest.py                 # 30 días, 1m
    python scripts/backtest.py --days 7
    python scripts/backtest.py --days 14 --timeframe 5m --symbol ETH/USDT

Reproduce la lógica del bot:
- IndicatorEngine para el snapshot
- TechnicalEngine para la decisión
- OrderManager simplificado para SL/TP/trailing
- Posiciones bidireccionales (LONG y SHORT)
- Riesgo: max_risk_per_trade * capital, ATR-based SL
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Path hack: importar desde el root del proyecto
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows: consola cp1252 no soporta emojis ni flechas. Usamos utf-8 con replace.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import ccxt
import pandas as pd

from config.settings import load_settings
from core.indicators import IndicatorEngine, MarketSnapshot
from core.technical_engine import TechnicalEngine
from core.mtf_context import build_mtf_context


# ───────── modelos internos ─────────

@dataclass
class SimPosition:
    direction: str                 # "LONG" | "SHORT"
    entry_price: float
    amount_btc: float
    amount_usdt: float
    stop_loss: float
    original_stop_loss: float
    take_profit: float
    entry_idx: int
    entry_reason: str
    trailing_active: bool = False
    extreme_price: float = 0.0     # max para LONG, min para SHORT
    # TP escalado: TP1 parcial + breakeven. initial_* guardan el tamaño original
    # (amount_* se reducen tras el parcial). realized_pnl acumula lo cobrado en TP1.
    initial_amount_btc: float = 0.0
    initial_amount_usdt: float = 0.0
    tp1_price: float = 0.0
    tp1_done: bool = False
    realized_pnl: float = 0.0


@dataclass
class Trade:
    direction: str
    entry_price: float
    exit_price: float
    amount_btc: float
    pnl_usdt: float
    pnl_pct: float
    bars_held: int
    entry_reason: str
    exit_reason: str
    trailing_was_active: bool
    took_tp1: bool = False


# ───────── descarga histórica ─────────

def fetch_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Trae N días de velas de Binance mainnet, paginando si es necesario."""
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})

    minutes_per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                       "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    total_bars = (days * 24 * 60) // minutes_per_bar

    end_ms = ex.milliseconds()
    start_ms = end_ms - (total_bars * minutes_per_bar * 60_000)

    all_rows: list[list] = []
    chunk_limit = 1000
    cursor = start_ms
    while cursor < end_ms:
        rows = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=chunk_limit)
        if not rows:
            break
        all_rows.extend(rows)
        cursor = rows[-1][0] + minutes_per_bar * 60_000
        if len(rows) < chunk_limit:
            break

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


# ───────── snapshot a partir del df pre-computado ─────────

def snapshot_at(df_with_indicators: pd.DataFrame, i: int, settings) -> MarketSnapshot:
    """
    Construye un MarketSnapshot mirando hasta la vela i (cerrada).
    Replica la lógica de IndicatorEngine._build_snapshot pero sin recalcular.
    El precio "actual" es el close de la vela i.
    """
    if i < 3:
        raise ValueError("Necesita al menos 3 velas")

    current_price = float(df_with_indicators.iloc[i]["close"])
    last = df_with_indicators.iloc[i]            # vela cerrada actual
    prev = df_with_indicators.iloc[i - 1]        # vela anterior cerrada

    minutes_per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                       "1h": 60, "4h": 240, "1d": 1440}.get(settings.timeframe, 15)
    candles_1h = max(1, 60 // minutes_per_bar)
    candles_24h = max(1, candles_1h * 24)
    price_1h_ago = (
        float(df_with_indicators.iloc[i - candles_1h]["close"])
        if i >= candles_1h else current_price
    )
    price_24h_ago = (
        float(df_with_indicators.iloc[i - candles_24h]["close"])
        if i >= candles_24h else current_price
    )
    price_change_1h = ((current_price - price_1h_ago) / price_1h_ago) * 100
    price_change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100

    macd_crossover = (
        float(last["macd"]) > float(last["macd_signal"]) and
        float(prev["macd"]) <= float(prev["macd_signal"])
    )

    ema50 = float(last["ema50"])
    ema200 = float(last["ema200"])

    bb_upper = float(last["bb_upper"]) if not pd.isna(last["bb_upper"]) else current_price
    bb_lower = float(last["bb_lower"]) if not pd.isna(last["bb_lower"]) else current_price
    bb_middle = float(last["bb_middle"]) if not pd.isna(last["bb_middle"]) else current_price
    bb_width = ((bb_upper - bb_lower) / bb_middle) * 100 if bb_middle else 0

    # Donchian de la vela i-1 (mismo criterio que IndicatorEngine)
    if i >= 2 and not pd.isna(df_with_indicators.iloc[i - 1]["donchian_high"]):
        donchian_high = float(df_with_indicators.iloc[i - 1]["donchian_high"])
        donchian_low = float(df_with_indicators.iloc[i - 1]["donchian_low"])
        donchian_mid = float(df_with_indicators.iloc[i - 1]["donchian_mid"])
    else:
        donchian_high = donchian_low = donchian_mid = current_price

    breakout_up = current_price > donchian_high
    breakout_down = current_price < donchian_low

    atr = float(last["atr"]) if not pd.isna(last["atr"]) else 0
    atr_pct = (atr / current_price) * 100 if current_price else 0
    adx_val = float(last["adx"]) if "adx" in df_with_indicators.columns and not pd.isna(last["adx"]) else 0.0

    # Tendencia
    if current_price > ema200 and ema50 > ema200:
        trend, strength = "BULL", min(abs((current_price - ema200) / ema200) * 100 / 5.0, 1.0)
    elif current_price < ema200 and ema50 < ema200:
        trend, strength = "BEAR", min(abs((current_price - ema200) / ema200) * 100 / 5.0, 1.0)
    else:
        trend, strength = "LATERAL", 0.3

    volume_current = float(last["volume"])
    volume_avg = float(last["volume_avg_20"]) if not pd.isna(last["volume_avg_20"]) else volume_current
    volume_ratio = volume_current / volume_avg if volume_avg > 0 else 1.0

    return MarketSnapshot(
        price=current_price,
        price_change_1h=round(price_change_1h, 3),
        price_change_24h=round(price_change_24h, 3),
        rsi=round(float(last["rsi"]) if not pd.isna(last["rsi"]) else 50, 2),
        rsi_prev=round(float(prev["rsi"]) if not pd.isna(prev["rsi"]) else 50, 2),
        macd_line=round(float(last["macd"]) if not pd.isna(last["macd"]) else 0, 4),
        macd_signal=round(float(last["macd_signal"]) if not pd.isna(last["macd_signal"]) else 0, 4),
        macd_histogram=round(float(last["macd_hist"]) if not pd.isna(last["macd_hist"]) else 0, 4),
        macd_crossover=macd_crossover,
        ema50=round(ema50, 2),
        ema200=round(ema200, 2),
        price_vs_ema50=round(((current_price - ema50) / ema50) * 100, 3) if ema50 else 0,
        price_vs_ema200=round(((current_price - ema200) / ema200) * 100, 3) if ema200 else 0,
        bb_upper=round(bb_upper, 2),
        bb_middle=round(bb_middle, 2),
        bb_lower=round(bb_lower, 2),
        bb_width=round(bb_width, 3),
        donchian_high=round(donchian_high, 2),
        donchian_low=round(donchian_low, 2),
        donchian_mid=round(donchian_mid, 2),
        breakout_up=breakout_up,
        breakout_down=breakout_down,
        distance_to_high_pct=round(((current_price - donchian_high) / donchian_high) * 100, 3) if donchian_high else 0,
        distance_to_low_pct=round(((current_price - donchian_low) / donchian_low) * 100, 3) if donchian_low else 0,
        atr=round(atr, 2),
        atr_pct=round(atr_pct, 4),
        adx=round(adx_val, 2),
        volume_current=round(volume_current, 4),
        volume_avg_20=round(volume_avg, 4),
        volume_ratio=round(volume_ratio, 3),
        trend=trend,
        trend_strength=round(strength, 3),
        symbol=settings.symbol,
        timeframe=settings.timeframe,
        candles_available=i + 1,
        is_warmed_up=i + 1 >= settings.warmup_candles,
    )


# ───────── motor de simulación ─────────

class Simulator:
    def __init__(self, settings):
        self.s = settings
        self.te = TechnicalEngine(settings)
        self.capital = settings.initial_capital
        self.position: Optional[SimPosition] = None
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[pd.Timestamp, float]] = []
        self.peak = self.capital
        self.max_dd = 0.0
        self.last_close_idx: int = -10**9

    def _effective_risk_pct(self) -> float:
        base = self.s.max_risk_per_trade
        if not self.s.use_kelly_sizing or len(self.trades) < self.s.kelly_min_trades:
            return base
        wins = [t.pnl_pct / 100 for t in self.trades if t.pnl_usdt > 0]
        losses = [abs(t.pnl_pct / 100) for t in self.trades if t.pnl_usdt <= 0]
        if not wins or not losses:
            return base
        wr = len(wins) / (len(wins) + len(losses))
        avg_w = sum(wins) / len(wins)
        avg_l = sum(losses) / len(losses)
        if avg_l <= 0:
            return base
        b = avg_w / avg_l
        full = wr - (1 - wr) / b
        if full <= 0:
            return self.s.kelly_min_risk_pct
        return max(
            self.s.kelly_min_risk_pct,
            min(self.s.kelly_max_risk_pct, full * self.s.kelly_fraction),
        )

    def _position_size_usdt(self, snap: MarketSnapshot, sl_pct: float) -> float:
        risk_pct = self._effective_risk_pct()
        max_risk = self.capital * risk_pct
        position = max_risk / sl_pct if sl_pct > 0 else max_risk * 10
        cap = self.capital * (1 - self.s.trade_reserve_pct)
        return min(position, cap)

    def _open(self, snap: MarketSnapshot, dec, idx: int) -> None:
        # Cooldown: no abrir si no pasaron suficientes velas desde el último cierre
        if self.s.cooldown_bars > 0 and (idx - self.last_close_idx) < self.s.cooldown_bars:
            return
        sl_pct = max(dec.stop_loss_pct, snap.atr_pct * self.s.atr_sl_multiplier / 100)
        tp_pct = dec.take_profit_pct
        size_usdt = self._position_size_usdt(snap, sl_pct)
        if size_usdt <= 0:
            return
        price = snap.price
        amt_btc = size_usdt / price
        if dec.direction == "SHORT":
            sl = price * (1 + sl_pct)
            tp = price * (1 - tp_pct)
        else:
            sl = price * (1 - sl_pct)
            tp = price * (1 + tp_pct)
        tp1 = 0.0
        if self.s.scaled_tp_enabled:
            tp1_dist = sl_pct * self.s.tp1_r_multiple
            tp1 = price * (1 - tp1_dist) if dec.direction == "SHORT" else price * (1 + tp1_dist)
        self.position = SimPosition(
            direction=dec.direction,
            entry_price=price,
            amount_btc=amt_btc,
            amount_usdt=size_usdt,
            stop_loss=sl,
            original_stop_loss=sl,
            take_profit=tp,
            entry_idx=idx,
            entry_reason=f"[{dec.razon[:80]}]",
            extreme_price=price,
            initial_amount_btc=amt_btc,
            initial_amount_usdt=size_usdt,
            tp1_price=tp1,
        )
        self.capital -= size_usdt

    def _take_partial_tp1(self, price: float, idx: int) -> None:
        """Cierra tp1_size_pct de la posición original en TP1 y mueve SL a breakeven."""
        p = self.position
        portion_btc = min(p.initial_amount_btc * self.s.tp1_size_pct, p.amount_btc)
        portion_usdt = p.initial_amount_usdt * self.s.tp1_size_pct
        if p.direction == "LONG":
            pnl = (price - p.entry_price) * portion_btc
        else:
            pnl = (p.entry_price - price) * portion_btc
        self.capital += portion_usdt + pnl
        p.realized_pnl += pnl
        p.amount_btc -= portion_btc
        p.amount_usdt -= portion_usdt
        p.tp1_done = True
        if self.s.breakeven_after_tp1:
            off = self.s.breakeven_offset_pct
            if p.direction == "LONG":
                be = p.entry_price * (1 + off)
                if be > p.stop_loss:
                    p.stop_loss = be
            else:
                be = p.entry_price * (1 - off)
                if be < p.stop_loss:
                    p.stop_loss = be

    def _close(self, exit_price: float, reason: str, idx: int) -> None:
        if self.position is None:
            return
        p = self.position
        if p.direction == "LONG":
            pnl = (exit_price - p.entry_price) * p.amount_btc
        else:
            pnl = (p.entry_price - exit_price) * p.amount_btc
        self.capital += p.amount_usdt + pnl
        # PnL total del trade = remanente + lo cobrado en TP1. El % se mide sobre
        # el notional original para que sea comparable con trades sin escalado.
        total_pnl = pnl + p.realized_pnl
        base_usdt = p.initial_amount_usdt or p.amount_usdt
        pnl_pct = total_pnl / base_usdt * 100 if base_usdt else 0.0
        if self.capital > self.peak:
            self.peak = self.capital
        dd = (self.peak - self.capital) / self.peak * 100
        if dd > self.max_dd:
            self.max_dd = dd
        self.trades.append(Trade(
            direction=p.direction,
            entry_price=p.entry_price,
            exit_price=exit_price,
            amount_btc=p.initial_amount_btc or p.amount_btc,
            pnl_usdt=total_pnl,
            pnl_pct=pnl_pct,
            bars_held=idx - p.entry_idx,
            entry_reason=p.entry_reason,
            exit_reason=reason,
            trailing_was_active=p.trailing_active,
            took_tp1=p.tp1_done,
        ))
        self.position = None
        self.last_close_idx = idx

    def _update_trailing(self, current_price: float, snap: MarketSnapshot) -> None:
        """
        Bidireccional. Si dynamic_trailing_enabled, usa la regla nueva:
          - Activa cuando profit cubre el SL original (R:R = 1:1).
          - En activación mueve SL a breakeven (free trade).
          - Después chasea el extreme a distancia (dynamic_trailing_atr_mult * ATR%).
        Si no, usa la regla histórica (activation_pct fijo, distance_pct fijo).
        """
        if not self.s.trailing_stop_enabled or self.position is None:
            return
        p = self.position

        if self.s.dynamic_trailing_enabled:
            atr_dist = max(0.003, snap.atr_pct * self.s.dynamic_trailing_atr_mult / 100)
            sl_pct_orig = abs(p.original_stop_loss - p.entry_price) / p.entry_price
            if p.direction == "LONG":
                if current_price > p.extreme_price:
                    p.extreme_price = current_price
                profit_pct = (current_price - p.entry_price) / p.entry_price
                if not p.trailing_active and profit_pct >= sl_pct_orig:
                    p.trailing_active = True
                    # SL a breakeven (free trade)
                    p.stop_loss = max(p.stop_loss, p.entry_price * 1.0005)
                if p.trailing_active:
                    new_sl = p.extreme_price * (1 - atr_dist)
                    if new_sl > p.stop_loss:
                        p.stop_loss = new_sl
            else:  # SHORT
                if current_price < p.extreme_price or p.extreme_price == p.entry_price:
                    p.extreme_price = current_price
                profit_pct = (p.entry_price - current_price) / p.entry_price
                if not p.trailing_active and profit_pct >= sl_pct_orig:
                    p.trailing_active = True
                    p.stop_loss = min(p.stop_loss, p.entry_price * 0.9995)
                if p.trailing_active:
                    new_sl = p.extreme_price * (1 + atr_dist)
                    if new_sl < p.stop_loss:
                        p.stop_loss = new_sl
            return

        # Modo legacy
        if p.direction == "LONG":
            if current_price > p.extreme_price:
                p.extreme_price = current_price
            profit_pct = (current_price - p.entry_price) / p.entry_price
            if not p.trailing_active and profit_pct >= self.s.trailing_activation_pct:
                p.trailing_active = True
            if p.trailing_active:
                new_sl = p.extreme_price * (1 - self.s.trailing_distance_pct)
                if new_sl > p.stop_loss:
                    p.stop_loss = new_sl
        else:  # SHORT
            if current_price < p.extreme_price or p.extreme_price == p.entry_price:
                p.extreme_price = current_price
            profit_pct = (p.entry_price - current_price) / p.entry_price
            if not p.trailing_active and profit_pct >= self.s.trailing_activation_pct:
                p.trailing_active = True
            if p.trailing_active:
                new_sl = p.extreme_price * (1 + self.s.trailing_distance_pct)
                if new_sl < p.stop_loss:
                    p.stop_loss = new_sl

    def step(self, snap: MarketSnapshot, bar: pd.Series, idx: int, ts, mtf=None) -> None:
        # 1) Si hay posición, intentar cerrar por SL/TP usando high/low de la vela
        if self.position is not None:
            self._update_trailing(snap.price, snap)
            p = self.position
            high = float(bar["high"])
            low = float(bar["low"])
            # Con trailing dinámico, ignoramos el TP fijo (let winners run).
            tp_enabled = not (
                self.s.dynamic_trailing_enabled and self.s.disable_fixed_tp_with_trailing
            )
            scaled = self.s.scaled_tp_enabled
            if p.direction == "LONG":
                if low <= p.stop_loss:
                    self._close(p.stop_loss, "Stop-loss" if not p.tp1_done else "Breakeven post-TP1", idx)
                else:
                    if scaled and not p.tp1_done and p.tp1_price and high >= p.tp1_price:
                        self._take_partial_tp1(p.tp1_price, idx)
                    if (self.position is not None and tp_enabled
                            and not p.trailing_active and high >= p.take_profit):
                        self._close(p.take_profit, "Take-profit", idx)
            else:  # SHORT
                if high >= p.stop_loss:
                    self._close(p.stop_loss, "Stop-loss" if not p.tp1_done else "Breakeven post-TP1", idx)
                else:
                    if scaled and not p.tp1_done and p.tp1_price and low <= p.tp1_price:
                        self._take_partial_tp1(p.tp1_price, idx)
                    if (self.position is not None and tp_enabled
                            and not p.trailing_active and low <= p.take_profit):
                        self._close(p.take_profit, "Take-profit", idx)

        # 2) Si no hay posición, evaluar señal de entrada
        if self.position is None:
            decision = self.te.analyze(snap, mtf=mtf)
            if decision.accion in ("COMPRAR", "VENDER"):
                self._open(snap, decision, idx)

        # 3) Equity tracking (capital + valor mark-to-market de la posición)
        if self.position is not None:
            p = self.position
            if p.direction == "LONG":
                upnl = (snap.price - p.entry_price) * p.amount_btc
            else:
                upnl = (p.entry_price - snap.price) * p.amount_btc
            equity = self.capital + p.amount_usdt + upnl
        else:
            equity = self.capital
        self.equity_curve.append((ts, equity))
        if equity > self.peak:
            self.peak = equity
        dd = (self.peak - equity) / self.peak * 100
        if dd > self.max_dd:
            self.max_dd = dd


# ───────── pre-computar indicadores en el df ─────────

def add_indicators(df: pd.DataFrame, settings) -> pd.DataFrame:
    eng = IndicatorEngine(settings)
    return eng._add_all_indicators(df.copy())


# ───────── reporte ─────────

def report(sim: Simulator, settings) -> None:
    initial = settings.initial_capital
    final = sim.equity_curve[-1][1] if sim.equity_curve else initial
    ret_pct = (final - initial) / initial * 100

    n = len(sim.trades)
    if n == 0:
        print("\n=== BACKTEST — sin operaciones ===")
        print(f"Capital final: ${final:,.2f} ({ret_pct:+.2f}%)")
        return

    wins = [t for t in sim.trades if t.pnl_usdt > 0]
    losses = [t for t in sim.trades if t.pnl_usdt <= 0]
    win_rate = len(wins) / n * 100
    avg_win = sum(t.pnl_usdt for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usdt for t in losses) / len(losses) if losses else 0
    profit_factor = (sum(t.pnl_usdt for t in wins) / abs(sum(t.pnl_usdt for t in losses))
                     if losses and sum(t.pnl_usdt for t in losses) != 0 else float("inf"))

    longs = [t for t in sim.trades if t.direction == "LONG"]
    shorts = [t for t in sim.trades if t.direction == "SHORT"]

    print("\n" + "=" * 60)
    print("  RESULTADO DEL BACKTEST")
    print("=" * 60)
    print(f"Capital inicial:   ${initial:,.2f}")
    print(f"Capital final:     ${final:,.2f}  ({ret_pct:+.2f}%)")
    print(f"Max drawdown:      {sim.max_dd:.2f}%")
    print()
    print(f"Total operaciones: {n}  ({len(longs)} LONG, {len(shorts)} SHORT)")
    print(f"Acertamos en:      {win_rate:.1f}%  ({len(wins)} ganadoras, {len(losses)} perdedoras)")
    print(f"Ganancia promedio: ${avg_win:+.2f}")
    print(f"Pérdida promedio:  ${avg_loss:+.2f}")
    print(f"Profit factor:     {profit_factor:.2f}")
    print()
    by_reason: dict[str, int] = {}
    for t in sim.trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    print("Cómo cerraron:")
    for r, c in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  • {r}: {c}")

    if settings.scaled_tp_enabled:
        tp1_count = sum(1 for t in sim.trades if t.took_tp1)
        print(f"\nTP1 parciales tomados: {tp1_count}/{n} trades")

    print()
    print("Últimas 5 operaciones:")
    for t in sim.trades[-5:]:
        emoji = "🟢" if t.pnl_usdt > 0 else "🔴"
        print(f"  {emoji} {t.direction} | entrada ${t.entry_price:,.2f} → salida ${t.exit_price:,.2f} "
              f"| {t.pnl_usdt:+.2f} USDT ({t.pnl_pct:+.2f}%) | {t.exit_reason} | {t.bars_held} velas")
    print("=" * 60)


# ───────── entrada ─────────

def main():
    parser = argparse.ArgumentParser(description="Backtest del bot")
    parser.add_argument("--days", type=int, default=30, help="Días de histórico")
    parser.add_argument("--timeframe", type=str, default=None, help="Override timeframe")
    parser.add_argument("--symbol", type=str, default=None, help="Override symbol")
    parser.add_argument("--cooldown", type=int, default=None, help="Cooldown en velas")
    parser.add_argument("--adx-min", type=float, default=None, help="ADX mínimo para operar")
    parser.add_argument("--macro-trend", action="store_true", help="Requerir EMA200 alineada")
    parser.add_argument("--kelly", action="store_true", help="Position sizing por Kelly")
    parser.add_argument("--mtf", action="store_true", help="Filtro de confluencia con TF 1h")
    parser.add_argument("--dyn-trailing", action="store_true", help="Trailing dinámico (sin TP fijo)")
    parser.add_argument("--scaled-tp", action="store_true", help="TP escalado: parcial en TP1 (R:R 1:1) + breakeven")
    args = parser.parse_args()

    settings = load_settings()
    if args.timeframe:
        settings.timeframe = args.timeframe
    if args.symbol:
        settings.symbol = args.symbol
    if args.cooldown is not None:
        settings.cooldown_bars = args.cooldown
    if args.adx_min is not None:
        settings.adx_min_trending = args.adx_min
    if args.macro_trend:
        settings.require_macro_trend = True
    if args.kelly:
        settings.use_kelly_sizing = True
    if args.mtf:
        settings.require_mtf_confluence = True
    if args.dyn_trailing:
        settings.dynamic_trailing_enabled = True
    if args.scaled_tp:
        settings.scaled_tp_enabled = True

    flags = []
    if settings.cooldown_bars > 0:
        flags.append(f"cooldown={settings.cooldown_bars}")
    if settings.adx_min_trending > 0:
        flags.append(f"adx>={settings.adx_min_trending:.0f}")
    if settings.require_macro_trend:
        flags.append("macro_trend")
    if settings.use_kelly_sizing:
        flags.append("kelly")
    if settings.require_mtf_confluence:
        flags.append("mtf")
    if settings.dynamic_trailing_enabled:
        flags.append("dyn-trailing")
    if settings.scaled_tp_enabled:
        flags.append(
            f"scaled-tp({settings.tp1_size_pct:.0%}@{settings.tp1_r_multiple:.1f}R)"
        )
    print(f"Mejoras activas: {', '.join(flags) if flags else 'ninguna (baseline)'}")

    print(f"Bajando {args.days} días de {settings.symbol} @ {settings.timeframe} de Binance mainnet...")
    df = fetch_history(settings.symbol, settings.timeframe, args.days)
    print(f"  → {len(df)} velas ({df.index[0]} → {df.index[-1]})")

    print("Calculando indicadores...")
    df = add_indicators(df, settings)

    df_1h = df_4h = None
    if settings.require_mtf_confluence:
        print("Bajando 1h y 4h para MTF...")
        df_1h = fetch_history(settings.symbol, "1h", max(args.days + 10, 30))
        df_4h = fetch_history(settings.symbol, "4h", max(args.days + 30, 60))
        print(f"  → 1h: {len(df_1h)} velas | 4h: {len(df_4h)} velas")

    print("Simulando...")
    sim = Simulator(settings)
    warmup = max(settings.warmup_candles, settings.ema_slow + 10)
    for i in range(warmup, len(df)):
        ts = df.index[i]
        try:
            snap = snapshot_at(df, i, settings)
        except Exception:
            continue
        mtf = None
        if df_1h is not None and df_4h is not None:
            # Slice histórico: hasta ts (inclusive). Asume índices ordenados.
            sub_1h = df_1h.loc[:ts]
            sub_4h = df_4h.loc[:ts]
            if len(sub_1h) > settings.ema_slow and len(sub_4h) > settings.ema_slow:
                mtf = build_mtf_context(sub_1h, sub_4h)
        sim.step(snap, df.iloc[i], i, ts, mtf=mtf)
    # Cerrar posición abierta al final, si la hay
    if sim.position is not None:
        last_price = float(df.iloc[-1]["close"])
        sim._close(last_price, "Fin del backtest", len(df) - 1)

    report(sim, settings)


if __name__ == "__main__":
    main()
