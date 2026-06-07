"""
scripts/audit_filters.py
Auditoría F — Aplicación incremental de filtros derivados del breakdown (audit E).

La auditoría E reveló que el motor tiene edge LATENTE en sub-regímenes pero
queda neutralizado por buckets perdedores en el agregado. Este script aplica
filtros de forma incremental y mide el efecto en el PnL/PF/DD para validar
si la edge sobrevive en un agregado tras filtrar:

  Filtro A: Cortar señales en hora 06-12 UTC (EU AM — el ladrón)
  Filtro B: A + ATR% ∈ [0.13%, 0.20%] (sweet spot de volatilidad)
  Filtro C: B + descartar volume spike > 4× (explosiones de noise reversibles)
  Filtro D: C + descartar Dom + Vie (días flojos)

⚠ Cherry-picking warning: estos filtros se derivaron del MISMO dataset que
ahora los valida. Esperar que se vea bonito acá. La prueba real es WFA
(auditoría #4) con out-of-sample.
"""

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import logging
logging.getLogger("core.scalping_engine").setLevel(logging.WARNING)

import pandas as pd
import ta

from config.settings import load_settings
from core.scalping_engine import ScalpingEngine
from scripts.backtest_scalping import fetch_history, Sim

# Silenciar el loguru de scalping_engine (usa logger global)
try:
    from logs.logger import logger as _lg
    _lg.remove()
    _lg.add(sys.stderr, level="WARNING")
except Exception:
    pass


FilterFn = Callable[[dict], bool]


def precompute_features(df: pd.DataFrame, atr_window: int, bb_window: int,
                          bb_dev: int) -> pd.DataFrame:
    """Pre-calcula ATR%, BB width y vol_ratio para TODO el df de una sola vez."""
    out = pd.DataFrame(index=df.index)
    atr = ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=atr_window,
    ).average_true_range()
    bb = ta.volatility.BollingerBands(close=df["close"], window=bb_window,
                                       window_dev=bb_dev)
    upper = bb.bollinger_hband()
    lower = bb.bollinger_lband()
    middle = bb.bollinger_mavg()
    vol_ma = df["volume"].rolling(bb_window).mean()
    out["atr_pct"] = (atr / df["close"]) * 100
    out["bb_width"] = (upper - lower) / middle
    out["vol_ratio"] = df["volume"] / vol_ma
    out["hour"] = df.index.hour
    out["weekday"] = df.index.weekday
    return out


def features_at(feats: pd.DataFrame, idx: int) -> dict:
    row = feats.iloc[idx]
    return {
        "atr_pct": float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else 0.0,
        "bb_width": float(row["bb_width"]) if pd.notna(row["bb_width"]) else 0.0,
        "vol_ratio": float(row["vol_ratio"]) if pd.notna(row["vol_ratio"]) else 0.0,
        "hour": int(row["hour"]),
        "weekday": int(row["weekday"]),
    }


# ─────────────────────────────────────────
#  Definición de filtros
# ─────────────────────────────────────────

def filter_a(f: dict) -> bool:
    """Cortar hora 06-12 UTC (EU AM)."""
    return not (6 <= f["hour"] < 12)


def filter_b(f: dict) -> bool:
    """A + ATR% ∈ [0.13%, 0.20%]."""
    return filter_a(f) and 0.13 <= f["atr_pct"] <= 0.20


def filter_c(f: dict) -> bool:
    """B + volume spike <= 4×."""
    return filter_b(f) and f["vol_ratio"] <= 4.0


def filter_d(f: dict) -> bool:
    """C + descartar Dom (6) y Vie (4)."""
    return filter_c(f) and f["weekday"] not in (4, 6)


def filter_none(f: dict) -> bool:
    return True


# ─────────────────────────────────────────
#  Backtest con filtro
# ─────────────────────────────────────────

def run_backtest_filtered(df: pd.DataFrame, settings, leverage: float,
                           filter_fn: FilterFn, feats: pd.DataFrame) -> Sim:
    engine = ScalpingEngine(settings)
    sim = Sim(settings, leverage=leverage)
    warm = 110
    rejected_count = 0
    accepted_count = 0
    for i in range(warm, len(df)):
        ts = df.index[i]
        sim.step(df.iloc[i], i, ts, engine=engine)
        if sim.position is None:
            dec = engine.analyze(df, i)
            if dec.accion in ("COMPRAR", "VENDER"):
                f = features_at(feats, i)
                if filter_fn(f):
                    sim.open(dec, i, ts)
                    accepted_count += 1
                else:
                    rejected_count += 1
    if sim.position is not None:
        sim.close(float(df.iloc[-1]["close"]), "Fin", len(df) - 1, df.index[-1], engine=engine)
    sim._stats = {"rejected": rejected_count, "accepted": accepted_count}
    return sim


def metrics(sim: Sim, initial_capital: float) -> dict:
    n = len(sim.trades)
    if n == 0:
        return {"n": 0, "wr": 0, "ret": 0, "dd": 0, "pf": 0, "avg": 0,
                 "total": 0, "fees": 0, "rejected": sim._stats["rejected"]}
    wins = [t for t in sim.trades if t.pnl_usdt > 0]
    losses = [t for t in sim.trades if t.pnl_usdt <= 0]
    total = sum(t.pnl_usdt for t in sim.trades)
    fees = sum(t.fee_paid for t in sim.trades)
    wr = len(wins) / n * 100
    pf = (sum(t.pnl_usdt for t in wins) / abs(sum(t.pnl_usdt for t in losses))
           if losses and sum(t.pnl_usdt for t in losses) != 0 else float("inf"))
    return {
        "n": n,
        "wr": wr,
        "ret": total / initial_capital * 100,
        "dd": sim.max_dd,
        "pf": pf,
        "avg": total / n,
        "total": total,
        "fees": fees,
        "rejected": sim._stats["rejected"],
    }


def main():
    ap = argparse.ArgumentParser(description="Aplicación incremental de filtros")
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

    filters = [
        ("Sin filtro (baseline)", filter_none),
        ("A: −EU AM (06-12 UTC)", filter_a),
        ("B: A + ATR%∈[0.13,0.20]", filter_b),
        ("C: B + vol_ratio≤4×", filter_c),
        ("D: C + −Dom/Vie", filter_d),
    ]

    print("\n" + "=" * 90)
    print("  AUDITORÍA F — APLICACIÓN INCREMENTAL DE FILTROS")
    print("=" * 90)
    print(f"{'Filtro':<26}{'n':>5}{'WR%':>7}{'avg$':>8}{'tot$':>9}"
          f"{'ret%':>8}{'DD%':>7}{'PF':>6}{'rejN':>7}")
    print("─" * 90)

    # Pre-calcular features UNA vez (no recalcular por cada filtro)
    feats = precompute_features(df, atr_window=14, bb_window=20, bb_dev=2)

    results = []
    for label, fn in filters:
        sim = run_backtest_filtered(df, s, args.leverage, fn, feats)
        m = metrics(sim, s.initial_capital)
        results.append((label, m))
        pf_str = f"{m['pf']:.2f}" if m["pf"] != float("inf") else "∞"
        print(f"{label:<26}{m['n']:>5}{m['wr']:>6.1f}%{m['avg']:>+8.2f}"
              f"{m['total']:>+9.2f}{m['ret']:>+8.2f}%{m['dd']:>6.2f}%"
              f"{pf_str:>6}{m['rejected']:>7}")

    print("─" * 90)

    # Δ vs baseline
    baseline = results[0][1]
    print(f"\nΔ vs BASELINE:")
    for label, m in results[1:]:
        d_ret = m["ret"] - baseline["ret"]
        d_dd = m["dd"] - baseline["dd"]
        d_n = m["n"] - baseline["n"]
        verdict = "✅" if m["ret"] > 0 and m["ret"] > baseline["ret"] else "🟡"
        if m["n"] < 15:
            verdict = "⚠️  muestra chica"
        print(f"  {label:<26}  Δret={d_ret:+.2f}pp  Δdd={d_dd:+.2f}pp  "
              f"Δn={d_n:+d}  {verdict}")

    print("\nNotas:")
    print("- 'rejN' = señales que el motor generó pero el filtro descartó")
    print("- Estos filtros NACEN del breakdown — el siguiente paso (WFA) los valida out-of-sample")
    print("- Si el filtro D no es claramente mejor que A, no merece la pena: keep it simple")


if __name__ == "__main__":
    main()
