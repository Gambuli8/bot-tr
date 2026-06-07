"""
scripts/audit_montecarlo.py
Auditoría de Edge vs Ruido (Bootstrap / Monte Carlo).

Metodología:
1) Corremos el backtest del ScalpingEngine UNA vez para obtener la serie
   real de PnLs por trade (PnL neto, después de fees, en USDT).
2) Permutamos aleatoriamente el orden de esos PnLs N veces (default 1000).
3) Para cada permutación reconstruimos la equity curve, calculamos:
       - Retorno final (en %)
       - Max Drawdown (en %)
4) Reportamos las distribuciones y, sobre todo:
       - P(retorno_final < 0)
       - P(max_dd > observed_max_dd)
       - P(retorno_final < threshold)  (probabilidad de ruina parcial)

LO QUE ESTAMOS PROBANDO:
El orden temporal real de los trades es UNA realización de muchas posibles
si la estrategia tiene edge. Permutar el orden simula "qué hubiera pasado
si los mismos trades hubieran salido en otro orden". Si la P(ruina) es alta,
el resultado positivo del backtest puede ser un artefacto del orden
particular en que vinieron los trades (suerte) y no edge real.

⚠ Limitación: este test ASUME que los PnLs por trade son i.i.d. (no hay
autocorrelación). Si la estrategia tiene clusters (ej. la racha de
ganancia/pérdida depende del régimen), el bootstrap subestima la cola.
Es complementario, no sustituye al walk-forward.
"""

import argparse
import random
import statistics
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

from config.settings import load_settings
from core.scalping_engine import ScalpingEngine
from scripts.backtest_scalping import fetch_history, Sim


def run_baseline_backtest(settings, days: int, timeframe: str,
                          leverage: float) -> list:
    """Corre el backtest scalping y devuelve la lista de pnl_usdt (neto, con fees)."""
    print(f"Bajando {days} días de {settings.symbol} @ {timeframe}...")
    df = fetch_history(settings.symbol, timeframe, days)
    print(f"  {len(df)} velas | risk={settings.max_risk_per_trade:.2%} | "
          f"leverage={leverage}x | fee_maker={settings.commission_pct_per_side:.4%}")
    engine = ScalpingEngine(settings)
    sim = Sim(settings, leverage=leverage)
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
    pnls = [t.pnl_usdt for t in sim.trades]
    return pnls, sim


def equity_curve_metrics(pnls: list, initial_capital: float) -> tuple[float, float]:
    """
    Dada una secuencia de pnls y un capital inicial, devuelve (retorno %, max_dd %).
    max_dd es el peor drawdown peak-to-trough en %.
    """
    eq = initial_capital
    peak = initial_capital
    max_dd = 0.0
    for pnl in pnls:
        eq += pnl
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    final = eq
    ret_pct = (final - initial_capital) / initial_capital * 100
    return ret_pct, max_dd


def monte_carlo(pnls: list, initial_capital: float,
                n_iter: int = 1000, seed: int = 42) -> dict:
    """
    Permutación aleatoria de los pnls. Devuelve distribuciones de
    retorno_final_% y max_dd_%.
    """
    rng = random.Random(seed)
    rets: list[float] = []
    dds: list[float] = []
    for _ in range(n_iter):
        shuffled = pnls[:]
        rng.shuffle(shuffled)
        ret, dd = equity_curve_metrics(shuffled, initial_capital)
        rets.append(ret)
        dds.append(dd)
    return {"returns": rets, "max_drawdowns": dds}


def pct(values: list, q: float) -> float:
    """Percentil (q en 0..1) por interpolación lineal."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def main():
    ap = argparse.ArgumentParser(description="Monte Carlo del ScalpingEngine")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--timeframe", type=str, default="5m")
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--risk-pct", type=float, default=0.08)
    ap.add_argument("--maker-fee", type=float, default=0.0002)
    ap.add_argument("--taker-fee", type=float, default=0.0005)
    ap.add_argument("--iterations", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ruin-threshold", type=float, default=-10.0,
                    help="Umbral de 'ruina parcial' en %% de retorno final (default -10%)")
    args = ap.parse_args()

    s = load_settings()
    s.max_risk_per_trade = args.risk_pct
    s.commission_pct_per_side = args.maker_fee
    setattr(s, "commission_taker_pct", args.taker_fee)

    # 1) Backtest real para obtener la serie de PnLs
    pnls, sim = run_baseline_backtest(s, args.days, args.timeframe, args.leverage)
    n_trades = len(pnls)
    if n_trades == 0:
        print("Sin trades en el período. Abortando.")
        return

    real_ret, real_dd = equity_curve_metrics(pnls, s.initial_capital)
    real_avg = sum(pnls) / n_trades
    real_std = statistics.stdev(pnls) if n_trades > 1 else 0.0
    real_wins = sum(1 for p in pnls if p > 0)
    real_wr = real_wins / n_trades * 100

    # 2) Monte Carlo
    print(f"\nCorriendo Monte Carlo: {args.iterations} permutaciones del orden...")
    mc = monte_carlo(pnls, s.initial_capital, n_iter=args.iterations, seed=args.seed)
    rets = mc["returns"]
    dds = mc["max_drawdowns"]

    # 3) Estadísticas
    p_negativo = sum(1 for r in rets if r < 0) / len(rets) * 100
    p_ruina = sum(1 for r in rets if r < args.ruin_threshold) / len(rets) * 100
    p_dd_supera = sum(1 for d in dds if d > real_dd) / len(dds) * 100

    print("\n" + "=" * 70)
    print("  AUDITORÍA #1 — MONTE CARLO (BOOTSTRAP DE ORDEN)")
    print("=" * 70)
    print(f"Trades simulados:      {n_trades}")
    print(f"WR real:               {real_wr:.1f}%  ({real_wins} ganadoras)")
    print(f"PnL promedio:          ${real_avg:+.3f}  (std ${real_std:.3f})")
    print()
    print(f"Backtest REAL:         retorno {real_ret:+.2f}%   max DD {real_dd:.2f}%")
    print()
    print(f"Distribución de RETORNOS finales tras {args.iterations} permutaciones:")
    print(f"  Min                  {min(rets):+.2f}%")
    print(f"  P5  (cola izq)       {pct(rets, 0.05):+.2f}%")
    print(f"  P25                  {pct(rets, 0.25):+.2f}%")
    print(f"  Mediana              {pct(rets, 0.50):+.2f}%")
    print(f"  P75                  {pct(rets, 0.75):+.2f}%")
    print(f"  P95  (cola der)      {pct(rets, 0.95):+.2f}%")
    print(f"  Max                  {max(rets):+.2f}%")
    print(f"  Promedio             {sum(rets)/len(rets):+.2f}%")
    print()
    print(f"Distribución de MAX DRAWDOWNS:")
    print(f"  Min                  {min(dds):.2f}%")
    print(f"  P5                   {pct(dds, 0.05):.2f}%")
    print(f"  Mediana              {pct(dds, 0.50):.2f}%")
    print(f"  P95  (cola peor)     {pct(dds, 0.95):.2f}%")
    print(f"  Max                  {max(dds):.2f}%")
    print()
    print("─" * 70)
    print(f"PROBABILIDADES (lo crítico):")
    print(f"  P(retorno_final < 0):                    {p_negativo:.1f}%")
    print(f"  P(retorno_final < {args.ruin_threshold:.0f}% [ruina parcial]):  {p_ruina:.1f}%")
    print(f"  P(max_dd > {real_dd:.2f}% [observado]):        {p_dd_supera:.1f}%")
    print("─" * 70)
    print()

    # Veredicto
    if p_negativo > 40:
        veredicto = "❌ EDGE NO CONFIABLE — alta P(ruina). Probable curve fitting."
    elif p_negativo > 25:
        veredicto = "⚠️  EDGE DÉBIL — al borde del ruido. Necesita más data o tuning."
    elif p_negativo > 10:
        veredicto = "🟡 EDGE MODESTO — positivo pero con cola gorda. Considerar Kelly fraccional bajo."
    else:
        veredicto = "✅ EDGE ROBUSTO — la estrategia tiene ventaja estadística clara."
    print(f"VEREDICTO: {veredicto}")
    print()
    print("Notas metodológicas:")
    print("- El test asume PnLs i.i.d. (sin autocorrelación). Si hay clustering,")
    print("  el shuffle ROMPE la dependencia → puede SUBestimar la cola gorda real.")
    print("- Para máxima robustez, complementar con Walk-Forward (auditoría #4).")
    print("- N de la muestra: con ~100 trades, el intervalo de confianza del WR")
    print(f"  es aprox ±{1.96 * (real_wr * (100 - real_wr) / n_trades) ** 0.5:.1f}pp.")


if __name__ == "__main__":
    main()
