"""
core/metrics.py
Métricas de performance de un backtest a partir de la lista de trades.
Reutilizable por cualquier motor (PA, Volatility Expansion, etc.).

Entrada: lista de trades con atributos/keys: entry_ts, exit_ts (datetime/Timestamp)
y pnl_usdt (float, neto de fees). Capital inicial.

Sharpe/Sortino se computan sobre retornos DIARIOS de la curva de equity (crypto
opera 365 días/año). Sin trades → métricas en cero.
"""

import math
from typing import Any

import pandas as pd


def _get(t: Any, key: str):
    if isinstance(t, dict):
        return t.get(key)
    return getattr(t, key, None)


def compute_metrics(trades: list, initial_capital: float,
                    periods_per_year: int = 365) -> dict:
    n = len(trades)
    base = {
        "trades": n, "return_pct": 0.0, "cagr_pct": 0.0, "profit_factor": 0.0,
        "win_rate_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0, "sortino": 0.0,
        "expectancy_usdt": 0.0, "expectancy_pct": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "final_capital": initial_capital,
    }
    if n == 0 or initial_capital <= 0:
        return base

    pnls = [float(_get(t, "pnl_usdt") or 0.0) for t in trades]
    total = sum(pnls)
    final = initial_capital + total
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / n * 100
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    expectancy = total / n
    return_pct = total / initial_capital * 100

    # Curva de equity ordenada por cierre.
    rows = []
    for t in trades:
        ts = _get(t, "exit_ts")
        rows.append((pd.Timestamp(ts), float(_get(t, "pnl_usdt") or 0.0)))
    rows.sort(key=lambda r: r[0])
    eq = initial_capital
    eq_points = []
    peak = initial_capital
    max_dd = 0.0
    for ts, pnl in rows:
        eq += pnl
        eq_points.append((ts, eq))
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # CAGR sobre el span de tiempo operado.
    first_ts = pd.Timestamp(_get(trades[0], "entry_ts") or rows[0][0])
    last_ts = rows[-1][0]
    days = max((last_ts - first_ts).days, 1)
    growth = final / initial_capital
    cagr = (growth ** (365.25 / days) - 1) * 100 if growth > 0 else -100.0

    # Sharpe/Sortino sobre retornos diarios de la curva de equity.
    eq_series = pd.Series({ts: v for ts, v in eq_points})
    eq_series = eq_series[~eq_series.index.duplicated(keep="last")].sort_index()
    daily = eq_series.resample("1D").last().ffill()
    daily_ret = daily.pct_change().dropna()
    sharpe = sortino = 0.0
    if len(daily_ret) > 2 and daily_ret.std() > 0:
        sharpe = daily_ret.mean() / daily_ret.std() * math.sqrt(periods_per_year)
        downside = daily_ret[daily_ret < 0]
        dstd = downside.std()
        if dstd and dstd > 0:
            sortino = daily_ret.mean() / dstd * math.sqrt(periods_per_year)

    return {
        "trades": n,
        "return_pct": round(return_pct, 2),
        "cagr_pct": round(cagr, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else 99.0,
        "win_rate_pct": round(wr, 1),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "expectancy_usdt": round(expectancy, 2),
        "expectancy_pct": round(expectancy / initial_capital * 100, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "final_capital": round(final, 2),
    }


def format_metrics(m: dict) -> str:
    return (
        f"Trades        {m['trades']}\n"
        f"Return        {m['return_pct']:+.2f}%\n"
        f"CAGR          {m['cagr_pct']:+.2f}%\n"
        f"Profit Factor {m['profit_factor']:.2f}\n"
        f"Win rate      {m['win_rate_pct']:.1f}%\n"
        f"Max DD        {m['max_drawdown_pct']:.2f}%\n"
        f"Sharpe        {m['sharpe']:.2f}\n"
        f"Sortino       {m['sortino']:.2f}\n"
        f"Expectancy    {m['expectancy_usdt']:+.2f} USDT/trade ({m['expectancy_pct']:+.3f}%)\n"
        f"Avg win/loss  {m['avg_win']:+.2f} / {m['avg_loss']:+.2f}"
    )
