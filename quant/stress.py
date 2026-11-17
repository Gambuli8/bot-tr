"""Fase 3: pruebas de estrés contra el sobreajuste.

1. Robustez de grilla: ¿qué fracción de TODAS las combinaciones de parámetros gana después de costos?
   Si sólo gana un rincón de la grilla, los parámetros "buenos" fueron suerte.
2. Walk-forward: optimizar en 12 meses, operar los 3 siguientes sin tocar nada, avanzar 3 meses, repetir.
   Sólo cuenta la curva fuera de muestra (OOS) encadenada.
3. Monte Carlo: bootstrap (con reposición) y permutación de los trades OOS → distribución de retorno final y
   drawdown. Responde "¿cuánto de esto es orden afortunado de los trades?".
4. Costos ×2: el walk-forward completo repetido con comisiones y slippage duplicados.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant import config as C
from quant import backtest as bt
from quant.strategies import STRATEGIES, signals


def precompute(name: str, df: pd.DataFrame) -> list[tuple[dict, pd.DataFrame]]:
    return [(p, signals(name, df, p)) for p in STRATEGIES[name].combos()]


def grid_scan(df: pd.DataFrame, sigs, costs: bt.Costs = bt.Costs()) -> pd.DataFrame:
    rows = []
    for p, s in sigs:
        st = bt.run(df, s, costs).stats
        rows.append(p | {k: st[k] for k in ("net_return", "cagr", "sharpe", "max_dd", "trades", "profit_factor")})
    return pd.DataFrame(rows)


def _objective(st: dict) -> float:
    if st["trades"] < C.MIN_TRAIN_TRADES:
        return -np.inf
    return st["sharpe"]


def optimize(df: pd.DataFrame, sigs, start: int, end: int, costs: bt.Costs) -> tuple[dict, pd.DataFrame, dict]:
    best = (-np.inf, None, None, None)
    for p, s in sigs:
        st = bt.run(df, s, costs, start=start, end=end).stats
        score = _objective(st)
        if score > best[0]:
            best = (score, p, s, st)
    if best[1] is None:  # ninguna combinación con suficientes trades: se toma la de más Sharpe igual
        p, s = sigs[0]
        return p, s, bt.run(df, s, costs, start=start, end=end).stats
    return best[1], best[2], best[3]


def walk_forward(df: pd.DataFrame, sigs, costs: bt.Costs = bt.Costs(),
                 train: int = C.WF_TRAIN_BARS, test: int = C.WF_TEST_BARS) -> dict:
    n = len(df)
    capital = C.INITIAL_CAPITAL_USDT
    folds, eq_parts, trade_parts = [], [], []
    s = 0
    while s + train + test // 2 <= n:  # un fold OOS de menos de 6 semanas no aporta información
        te_start, te_end = s + train, min(s + train + test, n)
        p, sig, is_st = optimize(df, sigs, s, te_start, costs)
        r = bt.run(df, sig, costs, capital=capital, start=te_start, end=te_end)
        folds.append({"test_from": df["dt"].iloc[te_start].date(), "test_to": df["dt"].iloc[te_end - 1].date(),
                      "params": p, "is_cagr": is_st["cagr"], "is_sharpe": is_st["sharpe"],
                      "oos_return": r.stats["net_return"], "oos_trades": r.stats["trades"],
                      "oos_max_dd": r.stats["max_dd"]})
        eq_parts.append(r.equity)
        if len(r.trades):
            trade_parts.append(r.trades)
        capital = float(r.equity.iloc[-1])
        s += test
    equity = pd.concat(eq_parts)
    trades = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame(columns=["pnl", "ret"])
    stats = bt.compute_stats(equity, trades, C.INITIAL_CAPITAL_USDT)
    is_cagr = float(np.mean([f["is_cagr"] for f in folds]))
    stats["wf_efficiency"] = stats["cagr"] / is_cagr if is_cagr > 0 else 0.0
    stats["is_cagr_mean"] = is_cagr
    stats["folds_positive"] = float(np.mean([f["oos_return"] > 0 for f in folds]))
    return {"stats": stats, "folds": folds, "equity": equity, "trades": trades}


def monte_carlo(trade_rets: np.ndarray, runs: int = C.MC_RUNS, seed: int = C.SEED) -> dict:
    """Bootstrap y permutación de retornos por trade (fracción del equity). Compuesto trade a trade."""
    rets = np.asarray(trade_rets, float)
    if len(rets) < 2:
        return {"prob_loss": 1.0, "final_p5": -1.0, "final_p50": 0.0, "dd_p50": 1.0, "dd_p95": 1.0,
                "prob_halt": 1.0, "perm_dd_p95": 1.0}
    rng = np.random.default_rng(seed)
    boot = rng.choice(rets, size=(runs, len(rets)), replace=True)
    perm = np.array([rng.permutation(rets) for _ in range(runs)])

    def paths(m):
        eq = np.cumprod(1 + m, axis=1)
        eq = np.hstack([np.ones((len(m), 1)), eq])
        dd = 1 - eq / np.maximum.accumulate(eq, axis=1)
        return eq[:, -1] - 1, dd.max(axis=1)

    fin, dd = paths(boot)
    _, pdd = paths(perm)
    return {"prob_loss": float((fin <= 0).mean()), "final_p5": float(np.percentile(fin, 5)),
            "final_p50": float(np.median(fin)), "dd_p50": float(np.median(dd)), "dd_p95": float(np.percentile(dd, 95)),
            "prob_halt": float((dd >= C.MAX_DRAWDOWN_HALT).mean()), "perm_dd_p95": float(np.percentile(pdd, 95))}


def evaluate_gates(grid: pd.DataFrame, wf: dict, mc: dict, wf_x2: dict) -> dict[str, tuple[float, bool]]:
    g, st = C.GATES, wf["stats"]
    return {
        "grid_positive_frac": (float((grid.net_return > 0).mean()), float((grid.net_return > 0).mean()) >= g["grid_positive_frac"]),
        "oos_net_return": (st["net_return"], st["net_return"] > g["oos_net_return"]),
        "oos_profit_factor": (st["profit_factor"], st["profit_factor"] >= g["oos_profit_factor"]),
        "oos_min_trades": (st["trades"], st["trades"] >= g["oos_min_trades"]),
        "wf_efficiency": (st["wf_efficiency"], st["wf_efficiency"] >= g["wf_efficiency"]),
        "mc_prob_loss": (mc["prob_loss"], mc["prob_loss"] < g["mc_prob_loss"]),
        "mc_dd95": (mc["dd_p95"], mc["dd_p95"] < g["mc_dd95"]),
        "cost_x2_net_return": (wf_x2["stats"]["net_return"], wf_x2["stats"]["net_return"] > g["cost_x2_net_return"]),
    }
