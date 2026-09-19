"""
Herramientas de línea de comandos.

  python -m bot.cli check
      Verifica .env, conexión y firma con BingX, saldo, modo de posición, y
      muestra para cada par cuánto apalancamiento y cantidad usaría con tu margen.

  python -m bot.cli replay --days 5 [--symbol BTC-USDT]
      Corre el motor sobre velas reales de los últimos días y muestra qué habría hecho (no opera).

  python -m bot.cli test-signal --event zone --symbol BTC-USDT --side LONG [--url http://127.0.0.1:8080]
      Manda una alerta de prueba al webhook (como si fuera TradingView) para ver
      el mensaje en Telegram. Los eventos 'entry' sólo se permiten en modo demo.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import requests

from bot.bingx import BingXClient, BingXError
from bot.config import load_settings
from bot.sizing import build_plan


def cmd_check() -> int:
    s = load_settings()
    errors = s.validate()
    print(f"Modo: {s.mode.upper()}  ·  Pares: {', '.join(s.symbols)}  ·  Margen: {s.margin_per_trade_usdt} USDT")
    if errors:
        print("❌ Configuración:\n  - " + "\n  - ".join(errors))
        if any("BINGX" in e for e in errors):
            return 1
    client = BingXClient(s.bingx_api_key, s.bingx_api_secret, s.mode)
    try:
        bal = client.balance()
        print(f"✅ Firma y API key OK · Saldo {bal['balance']:.2f} {bal['asset']} · disponible {bal['available']:.2f}")
        print(f"✅ Modo de posición: {'HEDGE (el bot lo pasa a one-way al arrancar)' if client.is_hedge_mode() else 'one-way'}")
        positions = client.positions()
        print(f"   Posiciones abiertas: {len(positions)}")
    except BingXError as exc:
        print(f"❌ BingX: {exc}")
        return 1

    print("\nSimulación de tamaño por par (SL -1.5% / TP +4.5%, LONG):")
    for sym in s.symbols:
        try:
            spec = client.contract(sym)
            price = client.price(sym)
        except BingXError as exc:
            print(f"  {sym:<10} ❌ {exc}")
            continue
        plan = build_plan(symbol=sym, direction="LONG", entry=price, stop_loss=price * 0.985,
                          take_profit=price * 1.045, spec=spec, margin_usdt=s.margin_per_trade_usdt,
                          max_leverage=s.max_leverage, min_rr=s.min_rr)
        if plan.ok:
            print(f"  {sym:<10} precio {price:<12g} → {plan.leverage:>2}x · qty {plan.qty_str:<8} · "
                  f"posición {plan.notional:6.2f} USDT · pierde {plan.risk_usdt:.3f} / gana {plan.reward_usdt:.3f}")
        else:
            print(f"  {sym:<10} ⚠️ {plan.reason}")
    return 0


def cmd_test_signal(args) -> int:
    s = load_settings()
    if args.event == "entry" and s.is_live:
        print("❌ Las entradas de prueba sólo se permiten en modo demo.")
        return 1
    client = BingXClient(s.bingx_api_key, s.bingx_api_secret, s.mode)
    price = client.price(args.symbol)
    long = args.side == "LONG"
    impulse = price * 0.04
    # Impulso armado para que el precio actual quede justo en el 0.618.
    end = price + impulse * 0.618 if long else price - impulse * 0.618
    start = end - impulse if long else end + impulse
    f618 = end - impulse * 0.618 if long else end + impulse * 0.618
    f75 = end - impulse * 0.75 if long else end + impulse * 0.75
    f786 = end - impulse * 0.786 if long else end + impulse * 0.786
    payload = {
        "secret": s.webhook_secret, "event": args.event,
        "id": f"TEST-{args.symbol}-{args.side[0]}-{int(time.time())}",
        "symbol": args.symbol, "side": args.side, "price": price, "time": int(time.time() * 1000),
        "fib_start": start, "fib_end": end, "fib_618": f618, "fib_75": f75, "fib_sl": f786,
        "zone_low": start * 0.995, "zone_high": start * 1.005, "note": "PRUEBA manual (no es una señal real)",
    }
    if args.event == "entry":
        payload["sl"] = f786
        payload["tp"] = end
    resp = requests.post(args.url.rstrip("/") + "/tv/webhook", json=payload, timeout=10)
    print(resp.status_code, resp.text)
    return 0 if resp.ok else 1


def cmd_replay(args) -> int:
    """Corre el motor sobre los últimos días de velas reales y lista lo que habría hecho (no opera)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bot.fmt import price
    from bot.sizing import build_plan
    from bot.strategy import StrategyParams, SymbolStrategy, build_bars, build_daily_zones, build_hourly

    s = load_settings()
    client = BingXClient(s.bingx_api_key, s.bingx_api_secret, s.mode)   # specs de contrato del modo actual
    market = BingXClient("", "", "live")                                # velas del mercado real
    tz = ZoneInfo(s.timezone)
    params = StrategyParams()
    symbols = [args.symbol] if args.symbol else s.symbols
    bars_5m = min(int(args.days * 288), 20000)
    totals = {"zone": 0, "choch": 0, "fib": 0, "cancel": 0, "entry": 0}
    results = []  # (symbol, pnl_usdt, r_multiple)
    for sym in symbols:
        zones = build_daily_zones(market.klines_history(sym, "1d", 400), params)
        hourly = build_hourly(market.klines_history(sym, "1h", int(args.days * 24) + 200), params)
        bars = build_bars(market.klines_history(sym, "5m", bars_5m), hourly, zones, params)
        engine = SymbolStrategy(sym, params)
        spec = client.contract(sym)
        busy_until = -1  # una posición por par, como el ejecutor
        print(f"\n=== {sym} · {len(bars)} velas 5m ===")
        for i, bar in enumerate(bars):
            for ev in engine.on_bar(bar):
                totals[ev["event"]] += 1
                if ev["event"] != "entry" and not args.verbose:
                    continue
                when = datetime.fromtimestamp(ev["time"] / 1000, tz).strftime("%d/%m %H:%M")
                line = f"  {when} {ev['side']:<5} {ev['event']:<6} {price(ev['price'])}  {ev['note']}"
                if ev["event"] == "entry":
                    plan = build_plan(symbol=sym, direction=ev["side"], entry=ev["price"], stop_loss=ev["sl"],
                                      take_profit=ev["tp"], spec=spec, margin_usdt=s.margin_per_trade_usdt,
                                      max_leverage=s.max_leverage, min_rr=s.min_rr)
                    if not plan.ok:
                        line += f" → NO opera: {plan.reason}"
                    elif i <= busy_until:
                        line += " → NO opera: ya había una operación abierta en el par"
                    else:
                        outcome, busy_until = _simulate(bars, i, ev["side"], ev["sl"], ev["tp"])
                        pnl = {"TP": plan.reward_usdt, "SL": -plan.risk_usdt}.get(outcome)
                        if pnl is not None:
                            results.append((sym, pnl, pnl / plan.risk_usdt))
                        line += (f" | ×{plan.leverage} SL {price(ev['sl'])} TP {price(ev['tp'])} R:R {plan.rr:.2f}"
                                 f" → {outcome}{'' if pnl is None else f' {pnl:+.3f} USDT'}")
                print(line)
        active = [f"{x.side} etapa {x.state}" for x in engine.setups() if x.state > 0]
        print(f"  en curso al final: {', '.join(active) or 'ninguno'}")

    print(f"\nEventos: {totals}")
    if results:
        wins = [r for r in results if r[1] > 0]
        net = sum(r[1] for r in results)
        print(f"Operaciones simuladas cerradas: {len(results)} · ganadas {len(wins)} "
              f"({len(wins) / len(results):.0%}) · resultado neto {net:+.3f} USDT · "
              f"promedio {sum(r[2] for r in results) / len(results):+.2f} R")
        for sym in symbols:
            rows = [r for r in results if r[0] == sym]
            if rows:
                print(f"  {sym:<10} {len(rows):>3} ops · ganadas {sum(1 for r in rows if r[1] > 0):>2} · "
                      f"{sum(r[1] for r in rows):+.3f} USDT")
        print("ADVERTENCIA: muestra chica, sin slippage ni límite diario/tope de posiciones. No valida la estrategia.")
    return 0


def _simulate(bars, entry_i: int, side: str, sl: float, tp: float) -> tuple[str, int]:
    """Qué tocó primero después de la entrada. Si SL y TP caen en la misma vela, se asume SL (conservador)."""
    for j in range(entry_i + 1, len(bars)):
        b = bars[j]
        hit_sl = b.low <= sl if side == "LONG" else b.high >= sl
        hit_tp = b.high >= tp if side == "LONG" else b.low <= tp
        if hit_sl:
            return "SL", j
        if hit_tp:
            return "TP", j
    return "ABIERTA", len(bars)


def main() -> None:
    parser = argparse.ArgumentParser(prog="bot.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    r = sub.add_parser("replay")
    r.add_argument("--days", type=float, default=5)
    r.add_argument("--symbol", default="")
    r.add_argument("--verbose", action="store_true", help="mostrar también zona, cambio 1H, 0,618 y cancelaciones")
    t = sub.add_parser("test-signal")
    t.add_argument("--event", choices=["zone", "choch", "fib", "cancel", "entry"], default="zone")
    t.add_argument("--symbol", default="BTC-USDT")
    t.add_argument("--side", choices=["LONG", "SHORT"], default="LONG")
    t.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    handlers = {"check": lambda: cmd_check(), "replay": lambda: cmd_replay(args), "test-signal": lambda: cmd_test_signal(args)}
    sys.exit(handlers[args.cmd]())


if __name__ == "__main__":
    main()
