"""
scripts/audit_trade_breakdown.py
Auditoría E — Disección de trades por subgrupos para encontrar el subset con edge.

El Monte Carlo (auditoría #1) declaró que el ScalpingEngine completo no tiene
edge: PnL promedio = -$0.078, retorno -2.36% en 60d. Pero un motor puede ser
perdedor en agregado y rentable en algún sub-régimen. Este script disecta los
trades por:

  - Hora UTC de entrada (4 bloques de 6h)
  - Día de la semana
  - Lado (LONG vs SHORT)
  - Régimen de ATR% (quartiles)
  - Régimen de BB width al entrar (quartiles)
  - Strength del volume spike (quartiles)
  - Duración del trade (bars_held)
  - Motivo de cierre (TP / SL / LIQUIDACIÓN / Fin)

Para cada bucket reporta:
  - n trades
  - WR (%)
  - PnL promedio ($)
  - PnL total ($)

LO QUE BUSCAMOS:
Un subset donde el PnL promedio sea positivo + significativo (n >= 10).
Si lo encontramos, el siguiente paso es agregar ese filtro al engine y
re-validar. Si NO encontramos ninguno, el motor BB squeeze 5m no tiene edge
en BTC y hay que cambiar el approach.

⚠ Sesgo de cherry-picking: cuantos más buckets miramos, mayor la chance de
encontrar uno positivo por puro ruido. Reglas para evitar self-deception:
  1. Sólo confiar en buckets con n >= 10
  2. PnL promedio debe ser > 1.5× std_error (significancia estadística)
  3. Confirmar después con WFA out-of-sample
"""

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
import ta

from config.settings import load_settings
from core.scalping_engine import ScalpingEngine
from scripts.backtest_scalping import fetch_history, Sim


def enrich_trades(df: pd.DataFrame, trades: list, bb_window: int, bb_dev: int,
                  atr_window: int) -> list[dict]:
    """Para cada trade, agrega features del estado del mercado en la entrada."""
    bb = ta.volatility.BollingerBands(close=df["close"], window=bb_window,
                                       window_dev=bb_dev)
    upper = bb.bollinger_hband()
    lower = bb.bollinger_lband()
    middle = bb.bollinger_mavg()
    bbw = (upper - lower) / middle

    atr = ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=atr_window,
    ).average_true_range()

    vol_ma = df["volume"].rolling(bb_window).mean()

    enriched = []
    for t in trades:
        try:
            idx = df.index.get_loc(t.entry_ts)
        except KeyError:
            continue
        if idx < 1:
            continue
        entry_bar = df.iloc[idx]
        close = float(entry_bar["close"])
        atr_now = float(atr.iloc[idx]) if pd.notna(atr.iloc[idx]) else 0.0
        bbw_now = float(bbw.iloc[idx]) if pd.notna(bbw.iloc[idx]) else 0.0
        vol_now = float(entry_bar["volume"])
        vol_ma_now = float(vol_ma.iloc[idx]) if pd.notna(vol_ma.iloc[idx]) else 0.0
        vol_ratio = vol_now / vol_ma_now if vol_ma_now > 0 else 0.0
        atr_pct = atr_now / close * 100 if close > 0 else 0.0

        enriched.append({
            "entry_ts": t.entry_ts,
            "direction": t.direction,
            "pnl": t.pnl_usdt,
            "pnl_gross": t.pnl_gross,
            "fee": t.fee_paid,
            "bars_held": t.bars_held,
            "exit_reason": t.exit_reason,
            "hour": t.entry_ts.hour,
            "weekday": t.entry_ts.weekday(),  # 0=Mon, 6=Sun
            "atr_pct": atr_pct,
            "bb_width": bbw_now,
            "vol_ratio": vol_ratio,
        })
    return enriched


def bucket_stats(trades: list[dict], key_fn, label: str, *, min_n: int = 1,
                  sort_buckets=None) -> None:
    """Imprime tabla de stats por bucket. key_fn(trade) -> bucket_label."""
    buckets: dict = {}
    for t in trades:
        k = key_fn(t)
        buckets.setdefault(k, []).append(t)

    print(f"\n── Por {label} ──")
    print(f"{'Bucket':<22}{'n':>5}{'WR%':>8}{'avgPnL$':>10}{'totPnL$':>10}{'stdPnL$':>10}{'t-stat':>8}")
    print("─" * 73)

    items = sort_buckets(buckets) if sort_buckets else sorted(buckets.items())
    for k, lst in items:
        n = len(lst)
        if n < min_n:
            continue
        wins = sum(1 for x in lst if x["pnl"] > 0)
        wr = wins / n * 100
        pnls = [x["pnl"] for x in lst]
        avg = sum(pnls) / n
        tot = sum(pnls)
        std = statistics.stdev(pnls) if n > 1 else 0.0
        # t-stat aproximado (un sample, H0: media = 0)
        t_stat = (avg / (std / n ** 0.5)) if std > 0 and n > 1 else 0.0
        marker = ""
        if n >= 10 and avg > 0 and t_stat > 1.5:
            marker = "  ✅"
        elif n >= 10 and avg > 0:
            marker = "  🟡"
        elif n >= 10 and avg < 0 and t_stat < -1.5:
            marker = "  ❌"
        print(f"{str(k):<22}{n:>5}{wr:>7.1f}%{avg:>+10.3f}{tot:>+10.2f}{std:>10.3f}{t_stat:>+8.2f}{marker}")


def quartile_buckets(values: list[float], n: int = 4) -> list[float]:
    """Devuelve los límites de los n quartiles ordenados."""
    s = sorted(values)
    if len(s) < n:
        return s
    bounds = []
    for i in range(1, n):
        idx = i * len(s) / n
        lo = int(idx)
        hi = min(lo + 1, len(s) - 1)
        frac = idx - lo
        bounds.append(s[lo] + (s[hi] - s[lo]) * frac)
    return bounds


def main():
    ap = argparse.ArgumentParser(description="Disección de trades del ScalpingEngine")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--timeframe", type=str, default="5m")
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--risk-pct", type=float, default=0.08)
    ap.add_argument("--maker-fee", type=float, default=0.0002)
    ap.add_argument("--taker-fee", type=float, default=0.0005)
    args = ap.parse_args()

    s = load_settings()
    s.max_risk_per_trade = args.risk_pct
    s.commission_pct_per_side = args.maker_fee
    setattr(s, "commission_taker_pct", args.taker_fee)

    print(f"Bajando {args.days} días de {s.symbol} @ {args.timeframe}...")
    df = fetch_history(s.symbol, args.timeframe, args.days)
    print(f"  {len(df)} velas | risk={s.max_risk_per_trade:.2%} | "
          f"leverage={args.leverage}x | fee_maker={s.commission_pct_per_side:.4%}")

    engine = ScalpingEngine(s)
    sim = Sim(s, leverage=args.leverage)
    warm = 110
    for i in range(warm, len(df)):
        ts = df.index[i]
        sim.step(df.iloc[i], i, ts, engine=engine)
        if sim.position is None:
            dec = engine.analyze(df, i)
            if dec.accion in ("COMPRAR", "VENDER"):
                sim.open(dec, i, ts)
    if sim.position is not None:
        sim.close(float(df.iloc[-1]["close"]), "Fin", len(df) - 1, df.index[-1], engine=engine)

    if not sim.trades:
        print("Sin trades. Abortando.")
        return

    enriched = enrich_trades(df, sim.trades,
                              bb_window=engine.bb_window,
                              bb_dev=engine.bb_dev,
                              atr_window=engine.atr_window)
    n = len(enriched)
    total_pnl = sum(t["pnl"] for t in enriched)
    wins = sum(1 for t in enriched if t["pnl"] > 0)
    wr = wins / n * 100

    print("\n" + "=" * 73)
    print("  AUDITORÍA E — DISECCIÓN DE TRADES")
    print("=" * 73)
    print(f"Trades totales:  {n}  |  WR global: {wr:.1f}%  |  PnL total: ${total_pnl:+.2f}")
    print(f"avg PnL: ${total_pnl/n:+.3f}  |  Esperanza matemática global: NEGATIVA"
          if total_pnl < 0 else
          f"avg PnL: ${total_pnl/n:+.3f}")

    # ─── Por lado ───
    bucket_stats(enriched, lambda t: t["direction"], "LADO (LONG vs SHORT)")

    # ─── Por hora UTC (bloques de 6h) ───
    def hour_block(t):
        h = t["hour"]
        if 0 <= h < 6:  return "00-06 (Asia)"
        if 6 <= h < 12: return "06-12 (EU AM)"
        if 12 <= h < 18: return "12-18 (US AM)"
        return "18-24 (US PM)"
    bucket_stats(enriched, hour_block, "HORA UTC (bloques de 6h)")

    # ─── Por día de la semana ───
    days_es = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    bucket_stats(enriched, lambda t: days_es[t["weekday"]], "DÍA DE LA SEMANA",
                  sort_buckets=lambda b: sorted(b.items(),
                                                key=lambda kv: days_es.index(kv[0])))

    # ─── Por motivo de cierre ───
    bucket_stats(enriched, lambda t: t["exit_reason"], "MOTIVO DE CIERRE")

    # ─── Por régimen de ATR% (quartiles) ───
    atr_vals = [t["atr_pct"] for t in enriched]
    atr_q = quartile_buckets(atr_vals, 4)
    def atr_bucket(t):
        v = t["atr_pct"]
        if v <= atr_q[0]: return f"Q1 ≤{atr_q[0]:.2f}%"
        if v <= atr_q[1]: return f"Q2 ≤{atr_q[1]:.2f}%"
        if v <= atr_q[2]: return f"Q3 ≤{atr_q[2]:.2f}%"
        return f"Q4 >{atr_q[2]:.2f}%"
    bucket_stats(enriched, atr_bucket, "RÉGIMEN ATR% (quartiles)")

    # ─── Por BB width (quartiles) ───
    bbw_vals = [t["bb_width"] for t in enriched]
    bbw_q = quartile_buckets(bbw_vals, 4)
    def bbw_bucket(t):
        v = t["bb_width"]
        if v <= bbw_q[0]: return f"Q1 ≤{bbw_q[0]:.4f}"
        if v <= bbw_q[1]: return f"Q2 ≤{bbw_q[1]:.4f}"
        if v <= bbw_q[2]: return f"Q3 ≤{bbw_q[2]:.4f}"
        return f"Q4 >{bbw_q[2]:.4f}"
    bucket_stats(enriched, bbw_bucket, "BB WIDTH al entrar (quartiles)")

    # ─── Por strength del volume spike (quartiles) ───
    vol_vals = [t["vol_ratio"] for t in enriched]
    vol_q = quartile_buckets(vol_vals, 4)
    def vol_bucket(t):
        v = t["vol_ratio"]
        if v <= vol_q[0]: return f"Q1 ≤{vol_q[0]:.2f}x"
        if v <= vol_q[1]: return f"Q2 ≤{vol_q[1]:.2f}x"
        if v <= vol_q[2]: return f"Q3 ≤{vol_q[2]:.2f}x"
        return f"Q4 >{vol_q[2]:.2f}x"
    bucket_stats(enriched, vol_bucket, "VOLUME SPIKE strength (quartiles)")

    # ─── Por duración (bars_held buckets) ───
    def dur_bucket(t):
        b = t["bars_held"]
        if b <= 3: return "1-3 velas"
        if b <= 10: return "4-10 velas"
        if b <= 30: return "11-30 velas"
        return ">30 velas"
    bucket_stats(enriched, dur_bucket, "DURACIÓN del trade")

    # ─── Top winners / losers ───
    print("\n── TOP 5 GANADORES ──")
    for t in sorted(enriched, key=lambda x: -x["pnl"])[:5]:
        print(f"  {t['entry_ts']}  {t['direction']:<5}  "
              f"ATR%={t['atr_pct']:.2f}  vol×{t['vol_ratio']:.2f}  "
              f"{t['exit_reason']:<12}  PnL=${t['pnl']:+.2f}")
    print("\n── TOP 5 PERDEDORES ──")
    for t in sorted(enriched, key=lambda x: x["pnl"])[:5]:
        print(f"  {t['entry_ts']}  {t['direction']:<5}  "
              f"ATR%={t['atr_pct']:.2f}  vol×{t['vol_ratio']:.2f}  "
              f"{t['exit_reason']:<12}  PnL=${t['pnl']:+.2f}")

    print("\n" + "─" * 73)
    print("LEYENDA: ✅ subset con edge significativo (n≥10, avgPnL>0, |t|>1.5)")
    print("         🟡 positivo pero no estadísticamente robusto")
    print("         ❌ subset claramente perdedor")
    print("─" * 73)


if __name__ == "__main__":
    main()
