"""Corre las fases 1–3 de punta a punta y deja el veredicto por escrito.

    python -m quant.research            # sincroniza datos, backtest, walk-forward, Monte Carlo, informe
    python -m quant.research --no-sync  # usa la base local

Salidas:
  docs/QUANT_RESEARCH.md                  informe legible (se versiona)
  data/quant/validated_strategy.json      lo que el bot tiene permitido operar (passed=true/false)
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from quant import backtest as bt
from quant import config as C
from quant import data
from quant import stress as X
from quant.strategies import STRATEGIES

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "QUANT_RESEARCH.md"

RULES = {
    "breakout": lambda p: (
        f"**Entrada:** la vela de 4h CIERRA por encima del máximo de las {p['n_entry']} velas anteriores → compra a "
        f"mercado en la apertura siguiente. **Stop inicial:** entrada − {p['atr_mult']} × ATR(14). "
        f"**Salida:** stop dinámico = mínimo de las últimas {p['n_exit']} velas (sólo sube; se ejecuta intrabar). "
        f"Tamaño: 1 % del equity en riesgo hasta el stop inicial."),
    "ema_cross": lambda p: (
        f"**Entrada:** EMA({p['fast']}) cruza por encima de EMA({p['slow']}) al cierre de 4h → compra en la apertura "
        f"siguiente. **Salida:** cierre con EMA({p['fast']}) < EMA({p['slow']}) → venta en la apertura siguiente. "
        f"**Stop:** entrada − {p['atr_mult']} × ATR(14)."),
    "rsi_reversion": lambda p: (
        f"**Entrada:** RSI({p['n']}) < {p['entry_lvl']} y cierre > SMA(200) → compra en la apertura siguiente. "
        f"**Salida:** RSI({p['n']}) > {p['exit_lvl']} al cierre. **Stop:** entrada − {p['atr_mult']} × ATR(14)."),
}

EDGE = {
    "breakout": "BTC tiene momentum de mediano plazo: los rompimientos de máximos de 1–2 semanas tienden a "
                "extenderse, y cortar rápido las falsas rupturas deja pérdidas chicas y ganancias largas.",
    "ema_cross": "Seguir la tendencia de varias semanas evita los grandes mercados bajistas a costa de entrar tarde.",
    "rsi_reversion": "Las sobreventas de corto plazo dentro de una tendencia alcista rebotan.",
}


def pct(x: float) -> str:
    return f"{x * 100:+.1f} %"


def run_all(df: pd.DataFrame) -> dict:
    out = {}
    for name in STRATEGIES:
        t0 = time.time()
        sigs = X.precompute(name, df)
        grid = X.grid_scan(df, sigs)
        default_sig = next(s for p, s in sigs if p == STRATEGIES[name].default) if any(
            p == STRATEGIES[name].default for p, _ in sigs) else X.signals(name, df, STRATEGIES[name].default)
        full = bt.run(df, default_sig)
        wf = X.walk_forward(df, sigs)
        mc = X.monte_carlo(wf["trades"]["ret"].to_numpy())
        wf_x2 = X.walk_forward(df, sigs, bt.Costs().scaled(2))
        gates = X.evaluate_gates(grid, wf, mc, wf_x2)
        live_params, _, live_is = X.optimize(df, sigs, len(df) - C.WF_TRAIN_BARS, len(df), bt.Costs())
        out[name] = {"grid": grid, "full": full, "wf": wf, "mc": mc, "wf_x2": wf_x2, "gates": gates,
                     "passed": all(ok for _, ok in gates.values()), "live_params": live_params,
                     "live_is": live_is, "secs": time.time() - t0}
        logging.info("%s listo en %.0fs — passed=%s", name, out[name]["secs"], out[name]["passed"])
    return out


def pick(results: dict) -> str:
    """Ganadora: la que pasa todos los filtros con mejor MAR fuera de muestra. Si ninguna pasa, se devuelve la
    más cercana (más filtros aprobados) SOLO como referencia: queda marcada passed=false."""
    return max(results, key=lambda n: (results[n]["passed"], sum(ok for _, ok in results[n]["gates"].values()),
                                       results[n]["wf"]["stats"]["mar"]))


def yearly(equity: pd.Series) -> dict:
    e = equity.groupby(equity.index.year).last()
    first = equity.iloc[0]
    prev = pd.concat([pd.Series([first]), e.iloc[:-1]]).to_numpy()
    return {int(y): float(v / p - 1) for y, v, p in zip(e.index, e.to_numpy(), prev)}


def write_report(df: pd.DataFrame, res: dict, winner: str) -> str:
    bh_full = bt.buy_and_hold(df)
    wf_start = int(df.index[df["dt"] >= res[winner]["wf"]["equity"].index[0]][0])
    bh_oos = bt.buy_and_hold(df, start=wf_start)
    W = res[winner]
    st, mc = W["wf"]["stats"], W["mc"]
    L = []
    a = L.append
    a(f"# Research cuantitativo — {C.SYMBOL} {C.TIMEFRAME} spot\n")
    a(f"Generado: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · datos {df['dt'].iloc[0]:%Y-%m-%d} → "
      f"{df['dt'].iloc[-1]:%Y-%m-%d} ({len(df)} velas, {int(df['synthetic'].sum())} reparadas) · Binance vía CCXT\n")
    a(f"Costos por lado: comisión {C.FEE_RATE:.2%} + slippage {C.SLIPPAGE_RATE:.2%} (ida y vuelta "
      f"≈ {2 * (C.FEE_RATE + C.SLIPPAGE_RATE):.2%}); funding {C.FUNDING_RATE_8H:.2%}/8h (spot). "
      f"Capital {C.INITIAL_CAPITAL_USDT:.0f} USDT, riesgo {C.RISK_PER_TRADE:.0%} por operación, "
      f"kill-switch diario −{C.MAX_DAILY_LOSS:.0%}, halt por drawdown −{C.MAX_DRAWDOWN_HALT:.0%}.\n")
    verdict = "✅ PASA todos los filtros" if W["passed"] else "❌ NINGUNA estrategia pasa todos los filtros"
    a(f"## Veredicto: {verdict}\n")
    a(f"Más cercana: **{STRATEGIES[winner].label}** "
      f"({sum(ok for _, ok in W['gates'].values())}/{len(W['gates'])} filtros).\n")

    a("## Fase 2 — backtest de muestra completa (parámetros por defecto, con costos)\n")
    a("| Estrategia | Neto | CAGR | Sharpe | Máx DD | Trades | Acierto | PF | Exposición | % grilla > 0 |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for n, r in res.items():
        s = r["full"].stats
        a(f"| {STRATEGIES[n].label} | {pct(s['net_return'])} | {pct(s['cagr'])} | {s['sharpe']:.2f} | "
          f"{s['max_dd']:.1%} | {s['trades']} | {s['win_rate']:.0%} | {s['profit_factor']:.2f} | "
          f"{s['exposure']:.0%} | {(r['grid'].net_return > 0).mean():.0%} |")
    a(f"| Comprar y mantener | {pct(bh_full['net_return'])} | {pct(bh_full['cagr'])} | {bh_full['sharpe']:.2f} | "
      f"{bh_full['max_dd']:.1%} | 1 | – | – | 100% | – |\n")

    a("## Fase 3 — walk-forward (12 m entrenamiento / 3 m fuera de muestra) + Monte Carlo\n")
    a("| Filtro (fijado antes de correr) | Umbral | " + " | ".join(STRATEGIES[n].label for n in res) + " |")
    a("|---|---|" + "---|" * len(res))
    thr = {"grid_positive_frac": "≥ 60 %", "oos_net_return": "> 0", "oos_profit_factor": "≥ 1,10",
           "oos_min_trades": "≥ 25", "wf_efficiency": "≥ 0,30", "mc_prob_loss": "< 10 %", "mc_dd95": "< 25 %",
           "cost_x2_net_return": "> 0"}
    names = {"grid_positive_frac": "Combinaciones de la grilla con neto > 0", "oos_net_return": "Retorno neto OOS",
             "oos_profit_factor": "Profit factor OOS", "oos_min_trades": "Trades OOS",
             "wf_efficiency": "Eficiencia walk-forward (CAGR OOS / IS)", "mc_prob_loss": "Monte Carlo: P(pérdida)",
             "mc_dd95": "Monte Carlo: DD percentil 95", "cost_x2_net_return": "OOS con costos ×2"}
    for g in thr:
        cells = []
        for n in res:
            v, ok = res[n]["gates"][g]
            txt = f"{v:.0f}" if g == "oos_min_trades" else (f"{v:.2f}" if g in ("oos_profit_factor", "wf_efficiency")
                                                            else f"{v:.1%}")
            cells.append(f"{'✅' if ok else '❌'} {txt}")
        a(f"| {names[g]} | {thr[g]} | " + " | ".join(cells) + " |")
    a("")

    a(f"## {STRATEGIES[winner].label}: detalle\n")
    a(f"**Edge en una oración:** {EDGE[winner]}\n")
    a(f"**Reglas exactas (parámetros elegidos con los últimos 12 meses):** {RULES[winner](W['live_params'])}\n")
    a("| Fuera de muestra (walk-forward) | Estrategia | Comprar y mantener |")
    a("|---|---|---|")
    a(f"| Período | {W['wf']['equity'].index[0]:%Y-%m-%d} → {W['wf']['equity'].index[-1]:%Y-%m-%d} | idem |")
    a(f"| Beneficio neto | {pct(st['net_return'])} ({st['final_equity'] - C.INITIAL_CAPITAL_USDT:+.0f} USDT) | "
      f"{pct(bh_oos['net_return'])} ({bh_oos['final_equity'] - C.INITIAL_CAPITAL_USDT:+.0f} USDT) |")
    a(f"| CAGR | {pct(st['cagr'])} | {pct(bh_oos['cagr'])} |")
    a(f"| Drawdown máximo | {st['max_dd']:.1%} | {bh_oos['max_dd']:.1%} |")
    a(f"| Sharpe | {st['sharpe']:.2f} | {bh_oos['sharpe']:.2f} |")
    a(f"| Trades / acierto / PF | {st['trades']} / {st['win_rate']:.0%} / {st['profit_factor']:.2f} | – |")
    a(f"| t-stat del retorno medio por trade | {st['t_stat']:.2f} | – |")
    a(f"| Folds OOS positivos | {st['folds_positive']:.0%} | – |\n")
    a("Retorno OOS por año: " + " · ".join(f"{y}: {pct(v)}" for y, v in yearly(W["wf"]["equity"]).items()) + "\n")
    a(f"Monte Carlo ({C.MC_RUNS} remuestreos de los {st['trades']} trades OOS): P(pérdida) {mc['prob_loss']:.1%} · "
      f"retorno p5 {pct(mc['final_p5'])} / mediana {pct(mc['final_p50'])} · DD mediana {mc['dd_p50']:.1%} / "
      f"p95 {mc['dd_p95']:.1%} · P(tocar el halt de −{C.MAX_DRAWDOWN_HALT:.0%}) {mc['prob_halt']:.1%}.\n")
    a("Parámetros elegidos en cada fold:\n")
    a("| OOS desde | hasta | Parámetros | Retorno OOS | Trades |")
    a("|---|---|---|---|---|")
    for f in W["wf"]["folds"]:
        a(f"| {f['test_from']} | {f['test_to']} | `{f['params']}` | {pct(f['oos_return'])} | {f['oos_trades']} |")
    a("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    df = data.load() if args.no_sync else data.sync()
    res = run_all(df)
    winner = pick(res)
    report = write_report(df, res, winner)
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(report, encoding="utf-8")
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "symbol": C.SYMBOL, "timeframe": C.TIMEFRAME,
        "data_until": df["dt"].iloc[-1].isoformat(), "strategy": winner, "params": res[winner]["live_params"],
        "passed": res[winner]["passed"],
        "gates": {k: {"value": float(v), "ok": bool(ok)} for k, (v, ok) in res[winner]["gates"].items()},
        "all": {n: {"passed": r["passed"], "failed": [k for k, (_, ok) in r["gates"].items() if not ok]}
                for n, r in res.items()},
    }
    C.VALIDATED_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(report)
    print(f"\nInforme: {DOC_PATH}\nValidación: {C.VALIDATED_PATH}")


if __name__ == "__main__":
    main()
