"""
scripts/backtest_trend_intraday.py
Trend-following sobre BTC en timeframe INTRADIA (1h/4h), reagrupando la data
real de 1-minuto (Bitstamp, GitHub ff137). Mismo motor que el diario, costos
reales de Binance Futures (0.04% taker + slippage).

Hallazgo: el punto dulce es 4h con Donchian lento (n40/e20): ~22 trades/año,
Calmar ~2.1, sobrevive los fees. Ir mas rapido (1h, n20) -> el ruido y los
costos destruyen el edge.

Uso:
  python scripts/backtest_trend_intraday.py --rule 4h --n 40 --exit-n 20 --by-year
"""
import argparse, gzip, io, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
from scripts.backtest_trend_port import run_portfolio

GZ = Path("/tmp/cryptocache/btc1min.csv.gz")


def load_btc(rule="4h", start="2021-01-01"):
    raw = gzip.decompress(GZ.read_bytes())
    df = pd.read_csv(io.BytesIO(raw))
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s")
    df = df.set_index("dt")
    df = df[df.index >= start]
    return df["close"].resample(rule).last().dropna().to_frame("btc")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", default="4h")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--exit-n", type=int, default=20)
    ap.add_argument("--atr-stop", type=float, default=6.0)
    ap.add_argument("--risk", type=float, default=0.02)
    ap.add_argument("--capital", type=float, default=300.0)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--by-year", action="store_true")
    a = ap.parse_args()

    px = load_btc(a.rule, a.start)
    m = run_portfolio(px, n=a.n, exit_n=a.exit_n, atr_stop=a.atr_stop,
                      risk=a.risk, long_only=True, capital=a.capital,
                      fee_side=0.0004, slip=0.0005)
    eq = m["eq"]
    yrs = (px.index[-1] - px.index[0]).days / 365.25
    print(f"\nBTC {a.rule}  Donchian {a.n}/{a.exit_n}  stop {a.atr_stop}xATR  "
          f"(fee 0.04%+slip)")
    print(f"  ${a.capital:.0f} -> ${eq.iloc[-1]:,.0f}   CAGR={m['cagr']:+.1f}%  "
          f"DD={m['dd']:.1f}%  Calmar={m['calmar']:.2f}  PF={m['pf']:.2f}")
    print(f"  trades={m['n']} (~{m['n']/yrs:.0f}/año)  WR={m['wr']:.0f}%")
    if a.by_year:
        for yr, sub in eq.groupby(eq.index.year):
            r = (sub.iloc[-1] / sub.iloc[0] - 1) * 100
            pk = sub.cummax(); dd = ((pk - sub) / pk).max() * 100
            print(f"    {yr}: ${sub.iloc[-1]:>8,.0f}  ({r:>+7.1f}%)  DD={dd:4.1f}%")


if __name__ == "__main__":
    main()
