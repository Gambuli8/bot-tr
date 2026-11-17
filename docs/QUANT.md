# Pipeline cuantitativo — BTC/USDT 4h spot (paper)

Independiente del bot de BingX (`bot/`). Todo vive en `quant/`. Resultados del research: `docs/QUANT_RESEARCH.md`.

| Archivo | Qué hace |
|---|---|
| `quant/config.py` | Mercado, costos y **límites de riesgo rígidos** (constantes; no se leen de `.env`) |
| `quant/data.py` | Fase 1: OHLCV vía CCXT (Binance), reparación de velas faltantes, SQLite (`data/quant/ohlcv.sqlite`) |
| `quant/strategies.py` | Fase 2: Donchian, cruce EMA, reversión RSI. El mismo código corre en el backtest y en el bot |
| `quant/backtest.py` | Motor vela a vela: señal al cierre → orden en la apertura siguiente, stops con gaps, comisión, slippage, funding |
| `quant/risk.py` | Sizing al 1 %, kill-switch diario −3 %, halt por drawdown −15 % (compartido backtest/bot) |
| `quant/stress.py` | Fase 3: robustez de grilla, walk-forward 12m/3m, Monte Carlo, costos ×2 |
| `quant/research.py` | Corre las fases 1–3 y escribe el informe + `data/quant/validated_strategy.json` |
| `quant/broker.py` | Paper broker: precios reales, órdenes simuladas, stop simulado como orden en el exchange |
| `quant/bot.py` | Fase 4: bucle autónomo con logs JSON, reintentos, estado atómico y kill-switches |

## Uso

```bash
pip install -r requirements-quant.txt
python -m quant.research          # baja/actualiza datos y valida (≈2 min)
python -m quant.bot               # sólo opera si la estrategia pasó TODOS los filtros
python -m quant.bot --candidate   # prueba forward en paper de la candidata no validada
python -m quant.bot --status
python -m pytest quant/tests -q
```

Desde nubes de EE.UU. Binance bloquea `api.binance.com`; por defecto se usa el espejo público
`data-api.binance.vision`. En el VPS o en Argentina: `BINANCE_DATA_HOST=api.binance.com`.

## Reglas de riesgo (en código, no configurables)

- Riesgo por operación: 1 % del equity hasta el stop inicial, **incluyendo** comisiones y slippage de salida.
- Spot sin apalancamiento: nocional máximo 95 % del equity. Stops a menos de 0,3 % o más de 20 %: entrada rechazada.
- Kill-switch diario: equity marcado a mercado −3 % desde las 00:00 UTC → cierra todo, sin entradas hasta el día siguiente.
- Halt: −15 % desde el máximo de equity → cierra todo, crea `data/quant/HALTED` y se apaga. Rearme manual:
  `python -m quant.bot --reset-halt`.
- Datos viejos (más de 2 velas de atraso) → no abre posiciones.
- Si el bot pierde la conexión, al volver revisa las velas de 1m del corte y ejecuta el stop donde lo habría hecho
  una orden stop real.

## Antes de pasar a dinero real (no implementado a propósito)

El modo real necesita una orden `STOP_LOSS_LIMIT` puesta **en Binance** en el momento de la entrada (no un stop
vigilado desde el bot) y una API key sin permiso de retiro y restringida a la IP del VPS. Sólo tiene sentido si
la estrategia pasa la validación y la prueba forward en paper confirma el backtest.
