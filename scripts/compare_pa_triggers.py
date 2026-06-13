"""
scripts/compare_pa_triggers.py
Corre el PriceActionEngine con los distintos gatillos sobre EXACTAMENTE los
mismos datos y imprime una tabla comparativa lado a lado.

Reusa fetch_history y Sim de backtest_price_action para no duplicar el motor.
Baja los datos 1h/4h UNA sola vez y evalúa cada variante sobre ellos.

Uso:
    python scripts/compare_pa_triggers.py --days 30
    python scripts/compare_pa_triggers.py --days 60 --leverage 7 --symbol BTC/USDT

Requiere acceso de red a la API de Binance (api.binance.com). Si tu entorno
usa allowlist de egress, agregá ese host antes de correr.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config.settings import load_settings
from core.price_action_engine import PriceActionEngine
from scripts.backtest_price_action import Sim, fetch_history


# Variantes a comparar: (etiqueta, overrides de settings)
VARIANTS = [
    ("sweep", {"pa_trigger_mode": "sweep"}),
    ("choch", {"pa_trigger_mode": "choch", "pa_choch_require_vol": False}),
    ("choch+vol", {"pa_trigger_mode": "choch", "pa_choch_require_vol": True}),
]


def run_variant(s, df_1h, df_4h, leverage, warmup=60):
    """Corre una variante sobre los datos dados y devuelve la Sim resultante."""
    engine = PriceActionEngine(s)
    sim = Sim(s, leverage=leverage)
    for i in range(warmup, len(df_1h)):
        ts = df_1h.index[i]
        sim.step(df_1h.iloc[i], i, ts)
        if sim.position is None:
            df_1h_so_far = df_1h.iloc[: i + 1]
            df_4h_so_far = df_4h.loc[:ts]
            if len(df_4h_so_far) < 30:
                continue
            decision = engine.analyze(df_1h_so_far, df_4h_so_far)
            if decision.accion in ("COMPRAR", "VENDER"):
                sim.open(decision, i, ts)
    if sim.position is not None:
        sim.close(float(df_1h.iloc[-1]["close"]), "Fin del backtest",
                  len(df_1h) - 1, df_1h.index[-1])
    return sim


def metrics(sim, initial_capital):
    """Extrae métricas clave de una Sim."""
    n = len(sim.trades)
    final = initial_capital + sum(t.pnl_usdt for t in sim.trades)
    ret_pct = (final - initial_capital) / initial_capital * 100
    if n == 0:
        return {"trades": 0, "wr": 0.0, "pf_net": 0.0, "ret_pct": ret_pct,
                "max_dd": sim.max_dd, "final": final}
    wins = [t for t in sim.trades if t.pnl_usdt > 0]
    losses = [t for t in sim.trades if t.pnl_usdt <= 0]
    wr = len(wins) / n * 100
    loss_sum = sum(t.pnl_usdt for t in losses)
    pf_net = (sum(t.pnl_usdt for t in wins) / abs(loss_sum)
              if loss_sum != 0 else float("inf"))
    return {"trades": n, "wr": wr, "pf_net": pf_net, "ret_pct": ret_pct,
            "max_dd": sim.max_dd, "final": final}


def print_table(results, days, symbol, leverage):
    print("\n" + "=" * 78)
    print(f"  COMPARACIÓN GATILLOS PRICE ACTION — {symbol} | {days}d | "
          f"1h trigger + 4h structure | leverage {leverage}x")
    print("=" * 78)
    hdr = f"{'Gatillo':<12}{'Trades':>8}{'Win%':>9}{'PF neto':>10}{'Retorno%':>11}{'MaxDD%':>10}"
    print(hdr)
    print("-" * len(hdr))
    for label, m in results:
        pf = "inf" if m["pf_net"] == float("inf") else f"{m['pf_net']:.2f}"
        print(f"{label:<12}{m['trades']:>8}{m['wr']:>8.1f}%{pf:>10}"
              f"{m['ret_pct']:>+10.2f}%{m['max_dd']:>9.2f}%")
    print("=" * 78)

    # Veredicto simple: mejor PF neto entre las que operaron algo
    traded = [(l, m) for l, m in results if m["trades"] > 0]
    if traded:
        best = max(traded, key=lambda x: (x[1]["pf_net"], x[1]["ret_pct"]))
        print(f"Mejor por PF neto: {best[0]}  "
              f"(PF {best[1]['pf_net']:.2f}, retorno {best[1]['ret_pct']:+.2f}%, "
              f"DD {best[1]['max_dd']:.2f}%)")
        baseline = dict(results).get("sweep")
        if baseline and best[0] != "sweep" and baseline["trades"] > 0:
            print("⚠ Una variante choch supera al sweep en este período. "
                  "Validá en más días/WFA antes de cambiar prod.")
        else:
            print("Sweep sigue siendo el mejor en este período — no cambiar el default.")
    print()


def main():
    p = argparse.ArgumentParser(description="Comparar gatillos del PriceActionEngine")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--symbol", type=str, default=None)
    p.add_argument("--leverage", type=float, default=1.0)
    p.add_argument("--risk-pct", type=float, default=None)
    p.add_argument("--commission", type=float, default=None)
    args = p.parse_args()

    s = load_settings()
    if args.symbol:
        s.symbol = args.symbol
    if args.risk_pct is not None:
        s.max_risk_per_trade = args.risk_pct
    if args.commission is not None:
        s.commission_pct_per_side = args.commission

    print(f"Bajando {args.days} días de {s.symbol} (1h y 4h)...")
    df_1h = fetch_history(s.symbol, "1h", args.days + 5)
    df_4h = fetch_history(s.symbol, "4h", args.days + 30)
    print(f"  1h: {len(df_1h)} velas | 4h: {len(df_4h)} velas | "
          f"risk={s.max_risk_per_trade:.2%} fee={s.commission_pct_per_side:.4%}")

    results = []
    for label, overrides in VARIANTS:
        for k, v in overrides.items():
            setattr(s, k, v)
        sim = run_variant(s, df_1h, df_4h, args.leverage)
        results.append((label, metrics(sim, s.initial_capital)))

    print_table(results, args.days, s.symbol, args.leverage)


if __name__ == "__main__":
    main()
