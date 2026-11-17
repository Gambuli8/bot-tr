"""Fase 4: bot autónomo de paper trading BTC/USDT 4h.

    python -m quant.bot                 # opera la estrategia de data/quant/validated_strategy.json (si passed=true)
    python -m quant.bot --candidate     # paper trading de la candidata NO validada (prueba forward, nunca real)
    python -m quant.bot --once          # una sola iteración (para probar)
    python -m quant.bot --status        # estado actual
    python -m quant.bot --reset-halt    # rearmar tras el halt por drawdown (decisión humana, a mano)

Bucle (cada POLL_S segundos):
  1. Velas de 1m desde la última revisión → ¿se tocó el stop? (simula una orden stop en el exchange)
  2. Precio actual → equity marcado a mercado → kill-switches (pérdida diaria, drawdown máximo)
  3. Si cerró una vela de 4h nueva → señales de la estrategia (mismo código que el backtest) → órdenes
  4. Estado a disco (escritura atómica)

El control de riesgo es Python determinista (quant/risk.py). Ningún LLM participa en el bucle.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime, timezone

import ccxt
import pandas as pd

from quant import config as C
from quant import data
from quant.broker import PaperBroker, PaperState, load_state, save_state
from quant.risk import RiskManager, RiskState
from quant.strategies import STRATEGIES, signals

POLL_S = 60
BAR_CLOSE_GRACE_MS = 15_000       # esperar 15 s tras el cierre de la vela antes de pedirla
WARMUP_BARS = 400                 # historia para indicadores (SMA/EMA 200 + margen)
MAX_CONSECUTIVE_FAILURES = 30     # ~30 min sin poder operar → salir con error (Docker lo reinicia)
STOP = False

log = logging.getLogger("quant.bot")


# ── Logging estructurado (JSON por línea) ───────────────────────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}

    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": datetime.fromtimestamp(r.created, timezone.utc).isoformat(timespec="seconds"),
             "level": r.levelname, "logger": r.name, "event": r.getMessage()}
        d.update({k: v for k, v in vars(r).items() if k not in self.STD})
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, default=str, ensure_ascii=False)


def setup_logging() -> None:
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = JsonFormatter()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for h in (logging.StreamHandler(sys.stdout),
              logging.handlers.RotatingFileHandler(C.LOG_PATH, maxBytes=10_000_000, backupCount=5, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)


def ev(event: str, **kw) -> None:
    log.info(event, extra=kw)


# ── Configuración validada ──────────────────────────────────────────────────────────────────────────────
def load_validated(allow_candidate: bool) -> tuple[str, dict]:
    if not C.VALIDATED_PATH.exists():
        sys.exit(f"No existe {C.VALIDATED_PATH}. Corré antes: python -m quant.research")
    v = json.loads(C.VALIDATED_PATH.read_text(encoding="utf-8"))
    if v["symbol"] != C.SYMBOL or v["timeframe"] != C.TIMEFRAME or v["strategy"] not in STRATEGIES:
        sys.exit("validated_strategy.json no corresponde a este mercado/estrategia")
    if not v["passed"] and not allow_candidate:
        failed = [k for k, g in v["gates"].items() if not g["ok"]]
        sys.exit(f"La estrategia '{v['strategy']}' NO pasó la validación ({', '.join(failed)}). Regla 3: descartada. "
                 "Para hacer SOLO paper trading de prueba forward usá --candidate.")
    age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(v["generated_at"])).days
    if age_days > 100:
        log.warning("validation_stale", extra={"age_days": age_days, "hint": "re-correr python -m quant.research"})
    return v["strategy"], v["params"]


# ── Bucle ───────────────────────────────────────────────────────────────────────────────────────────────
def last_closed_bar_open(now_ms: int) -> int:
    return (now_ms // C.BAR_MS) * C.BAR_MS - C.BAR_MS


def step(ex, broker: PaperBroker, risk: RiskManager, strat: str, params: dict) -> None:
    s = broker.s
    now_ms = int(time.time() * 1000)

    # 1) stop contra velas de 1m (cubre también períodos sin conexión)
    if s.position:
        since = s.last_check_ms or now_ms - 5 * 60_000
        m1 = data.fetch_ohlcv_range(ex, since - 60_000, now_ms, timeframe="1m")
        t = broker.check_stop_candles(m1[["ts", "open", "high", "low", "close", "volume"]].values.tolist())
        if t:
            ev("stop_filled", **t)

    # 2) precio actual + kill-switches
    last = float(data.with_retry(ex.fetch_ticker, C.SYMBOL)["last"])
    if s.position and (t := broker.check_stop_price(last, now_ms)):
        ev("stop_filled", **t)
    equity = broker.equity(last)
    risk.on_new_day(datetime.now(timezone.utc).strftime("%Y-%m-%d"), equity)
    action = risk.check(equity, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if action:
        t = broker.sell(last, now_ms, action)
        log.error("kill_switch", extra={"action": action, "equity": equity, "trade": t, "risk": risk.s.to_dict()})
        if action == "halt":
            C.HALT_PATH.write_text(json.dumps({"when": now_ms, "equity": equity}), encoding="utf-8")

    # 3) vela de 4h nueva
    bar_ts = last_closed_bar_open(now_ms)
    if bar_ts > s.last_bar_ts and now_ms >= bar_ts + C.BAR_MS + BAR_CLOSE_GRACE_MS:
        raw = data.fetch_ohlcv_range(ex, bar_ts - WARMUP_BARS * C.BAR_MS, now_ms)
        df, n_fix = data.repair_gaps(raw)
        if df.empty or int(df["ts"].iloc[-1]) < bar_ts - (C.MAX_DATA_STALENESS_BARS - 1) * C.BAR_MS:
            log.warning("stale_data", extra={"expected_bar": bar_ts, "got": int(df["ts"].iloc[-1]) if len(df) else None})
            return
        sig = signals(strat, df, params).iloc[-1]
        row = df.iloc[-1]
        ev("bar_closed", bar=datetime.fromtimestamp(row.ts / 1000, timezone.utc).isoformat(), close=row.close,
           entry=bool(sig.entry), exit=bool(sig.exit), trail=None if pd.isna(sig.trail) else float(sig.trail),
           repaired=n_fix, equity=round(broker.equity(last), 2), in_position=bool(s.position))
        if s.position:
            if not pd.isna(sig.trail) and broker.raise_stop(float(sig.trail)):
                ev("stop_raised", stop=s.position["stop"])
            if sig.exit:
                ev("exit_filled", **broker.sell(last, now_ms, "signal_exit"))
        elif sig.entry:
            ok, why = risk.can_enter()
            if not ok:
                ev("entry_blocked", reason=why)
            elif int(row.ts) != bar_ts:
                ev("entry_blocked", reason="signal_bar_not_latest")
            else:
                filled, info = broker.buy(last, float(sig.stop_dist), now_ms)
                ev("entry_filled" if filled else "entry_rejected", info=info)
        s.last_bar_ts = bar_ts

    s.risk = risk.s.to_dict()


def run(args) -> int:
    setup_logging()
    if C.HALT_PATH.exists():
        log.error("halted", extra={"file": str(C.HALT_PATH), "hint": "revisar y usar --reset-halt"})
        return 2
    strat, params = load_validated(args.candidate)
    state = load_state()
    broker, risk = PaperBroker(state), RiskManager(RiskState.from_dict(state.risk))
    ex = data.make_exchange()
    ev("bot_start", strategy=strat, params=params, candidate=args.candidate, cash=state.cash,
       position=state.position, risk_per_trade=C.RISK_PER_TRADE, max_daily_loss=C.MAX_DAILY_LOSS,
       max_dd_halt=C.MAX_DRAWDOWN_HALT, mode="paper")

    def _stop(signum, _frame):
        global STOP
        STOP = True
        ev("signal_received", signum=signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    failures = 0
    while not STOP:
        try:
            step(ex, broker, risk, strat, params)
            failures = 0
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout, ccxt.DDoSProtection) as e:
            failures += 1
            log.warning("network_error", extra={"err": f"{type(e).__name__}: {str(e)[:200]}", "failures": failures})
        except ccxt.ExchangeError as e:
            failures += 1
            log.error("exchange_error", extra={"err": str(e)[:300], "failures": failures})
        except Exception:
            failures += 1
            log.exception("unexpected_error", extra={"failures": failures})
        finally:
            save_state(state)
        if risk.s.halted:
            log.error("halted_exit", extra={"risk": risk.s.to_dict()})
            return 2
        if args.once:
            break
        if failures >= MAX_CONSECUTIVE_FAILURES:
            log.error("too_many_failures_exit", extra={"failures": failures})
            return 1
        delay = POLL_S if failures == 0 else min(POLL_S * 2 ** min(failures, 4), 600)
        for _ in range(int(delay)):
            if STOP:
                break
            time.sleep(1)
    ev("bot_stop", equity_cash=state.cash, position=state.position)
    return 0


def status() -> int:
    st = load_state()
    eq_trades = sum(t["pnl"] for t in st.trades)
    print(json.dumps({"cash": st.cash, "position": st.position, "closed_trades": len(st.trades),
                      "realized_pnl": round(eq_trades, 4), "risk": st.risk, "halted_file": C.HALT_PATH.exists(),
                      "last_bar": datetime.fromtimestamp(st.last_bar_ts / 1000, timezone.utc).isoformat()
                      if st.last_bar_ts else None}, indent=2, default=str))
    return 0


def reset_halt() -> int:
    st = load_state()
    if st.position:
        print("Hay una posición abierta: no se rearma con posición abierta.")
        return 1
    rs = RiskState.from_dict(st.risk)
    rs.halted, rs.peak_equity = False, st.cash
    rs.events.append({"type": "manual_reset", "when": datetime.now(timezone.utc).isoformat(), "equity": st.cash})
    st.risk = rs.to_dict()
    save_state(st)
    C.HALT_PATH.unlink(missing_ok=True)
    print(f"Halt rearmado. Nuevo pico de equity = {st.cash:.2f} USDT")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", action="store_true", help="paper trading de una estrategia NO validada")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset-halt", action="store_true")
    a = ap.parse_args()
    if a.status:
        sys.exit(status())
    if a.reset_halt:
        sys.exit(reset_halt())
    sys.exit(run(a))


if __name__ == "__main__":
    main()
