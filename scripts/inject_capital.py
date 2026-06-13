"""
scripts/inject_capital.py
Inyecta (o ajusta) capital en el state.json de un bot SIN perder el historial.

POR QUÉ EXISTE (Hallazgo 4 del HANDOFF):
El OrderManager lee `data/<sym>/state.json` al startup y usa el `capital` de ahí.
`INITIAL_CAPITAL` del .env SOLO se aplica si NO existe state.json. Entonces, para
sumar plata al wallet (ej. +$300) sin borrar el state (que perdería trade_journal,
capital actual, etc.), hay que editar el state.json correctamente.

Este script suma el monto a los cuatro anclajes para que las métricas queden
coherentes tras la inyección:
  - capital            → saldo operable actual (sube por la plata nueva)
  - capital_initial    → ancla del retorno % (sube: el retorno se mide vs total aportado)
  - capital_peak       → pico para el max drawdown (sube: la plata nueva no es "drawdown")
  - daily_capital_start→ base del límite de drawdown diario (sube: no dispara falso stop)

NO toca: open_position(s), total_trades, winning_trades, last_reset_date, etc.

Uso (SIEMPRE dry-run primero):
    python scripts/inject_capital.py --state data/btc/state.json --amount 100
    python scripts/inject_capital.py --state data/btc/state.json --amount 100 --apply

Para los 3 bots a la vez (ejemplo +$100 c/u), correr 3 veces con --apply.
"""

import argparse
import json
import sys
from pathlib import Path

# Consola Windows (cp1252) no puede encodear emojis; en el VPS (Linux + UTF-8)
# no hace falta, pero esto evita el crash al correrlo en local.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Campos que representan capital y deben subir con la inyección.
_CAPITAL_FIELDS = ("capital", "capital_initial", "capital_peak", "daily_capital_start")


def load_state(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"❌ No existe {path}. ¿Ruta correcta? (ej. data/btc/state.json)")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"❌ No pude leer {path}: {e}")


def inject(state: dict, amount: float) -> dict:
    """Devuelve una copia del state con el capital ajustado en `amount`."""
    new = dict(state)
    for field in _CAPITAL_FIELDS:
        current = float(new.get(field, 0.0) or 0.0)
        new[field] = round(current + amount, 8)
    return new


def main() -> None:
    p = argparse.ArgumentParser(description="Inyectar capital en state.json sin perder historial")
    p.add_argument("--state", required=True, help="Ruta al state.json (ej. data/btc/state.json)")
    p.add_argument("--amount", required=True, type=float,
                   help="Monto a sumar en USDT (puede ser negativo para retirar)")
    p.add_argument("--apply", action="store_true",
                   help="Aplica los cambios. Sin este flag es dry-run (no escribe).")
    args = p.parse_args()

    if args.amount == 0:
        sys.exit("❌ --amount 0 no hace nada.")

    path = Path(args.state)
    state = load_state(path)

    # Sanity: confirmar que parece un state.json válido del bot
    missing = [f for f in _CAPITAL_FIELDS if f not in state]
    if missing:
        sys.exit(
            f"❌ {path} no parece un state.json del bot (faltan campos: {missing}). "
            f"Aborto por seguridad."
        )

    new = inject(state, args.amount)

    verb = "Sumando" if args.amount > 0 else "Retirando"
    print(f"\n{verb} ${abs(args.amount):,.2f} en {path}\n")
    print(f"{'Campo':<22} {'Antes':>14} {'Después':>14}")
    print("-" * 52)
    for field in _CAPITAL_FIELDS:
        print(f"{field:<22} {float(state.get(field, 0)):>14,.2f} {float(new[field]):>14,.2f}")
    # Mostrar que el resto queda intacto
    pos = state.get("open_positions") or ([state["open_position"]] if state.get("open_position") else [])
    print(f"\nPosiciones abiertas (intactas): {len(pos)}")
    print(f"Trades totales (intactos):      {state.get('total_trades', 0)}")

    if not args.apply:
        print("\n🔎 DRY-RUN — no se escribió nada. Recorré con --apply para aplicar.")
        return

    # Backup antes de escribir (plata real)
    backup = path.with_suffix(".json.bak")
    backup.write_text(json.dumps(state, indent=2), encoding="utf-8")
    path.write_text(json.dumps(new, indent=2), encoding="utf-8")
    print(f"\n✅ Aplicado. Backup del estado previo en {backup}")
    print("   Reiniciá el bot para que tome el capital nuevo:")
    print("   docker compose -f docker-compose.multi.yml up -d")


if __name__ == "__main__":
    main()
