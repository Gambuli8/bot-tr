"""
Herramientas de línea de comandos.

  python -m bot.cli check
      Verifica .env, conexión y firma con BingX, saldo, modo de posición, y
      muestra para cada par cuánto apalancamiento y cantidad usaría con tu margen.

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="bot.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    t = sub.add_parser("test-signal")
    t.add_argument("--event", choices=["zone", "choch", "fib", "cancel", "entry"], default="zone")
    t.add_argument("--symbol", default="BTC-USDT")
    t.add_argument("--side", choices=["LONG", "SHORT"], default="LONG")
    t.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    sys.exit(cmd_check() if args.cmd == "check" else cmd_test_signal(args))


if __name__ == "__main__":
    main()
