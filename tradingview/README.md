# TradingView — Pine Script del PriceActionEngine

`pa_engine_strategy.pine` es el **port a Pine Script v5** del motor que opera el
bot en producción ([core/price_action_engine.py](../core/price_action_engine.py)).
Sirve para **ver la estrategia dibujada en el gráfico** y correr un backtest
visual en el *Strategy Tester* de TradingView.

## Cómo cargarlo

1. Abrí [TradingView](https://www.tradingview.com) → un gráfico de **BTC/USDT,
   SOL/USDT, AVAX/USDT o LINK/USDT** (el portafolio aprobado por WFA).
2. Poné el chart en **timeframe 1h** (el TF del gatillo del bot).
3. Abrí el **Pine Editor** (abajo), pegá el contenido de `pa_engine_strategy.pine`
   y dale **"Add to chart"**.
4. Mirá la pestaña **Strategy Tester** para el backtest, y el gráfico para los
   triángulos de señal (▲ LONG / ▼ SHORT) y el sombreado de estructura 4h
   (verde = BULL, rojo = BEAR, sin color = RANGE → no opera).

## Qué replica (fiel al engine)

| Concepto | Bot (Python) | Pine |
|---|---|---|
| Swings | Fractal Williams n=3 | `ta.pivothigh/low(n,n)` |
| Estructura 4h | últimos 2 HH+HL / LH+LL | `request.security("240", …)` |
| Gatillo | liquidity sweep en 1h a favor de 4h | `low<swingLow & close>swingLow` (y simétrico) |
| Volumen | vela > 1.5× MA(20), excluye la actual | `volume > volMult × ta.sma(volume,20)[1]` |
| SL | mecha del sweep ± 1.5×ATR | `low - atrSlMult×ta.atr(14)` |
| TP | R:R 1:2.5 | `entry + tpRR×riesgo` |
| Bounds SL | 0.3% – 5% del entry | `slMinPct / slMaxPct` |
| Sizing | riesgo 8%, cap por leverage | `riskPct` capeado por `leverage` |

Los **inputs del script están mapeados a las variables del `.env`/`settings.py`**
(ver comentarios en el `.pine`), así que podés calibrar y comparar.

## ⚠️ Importante — no confundir con el WFA

El Strategy Tester de TradingView **no es** nuestra validación estadística:

- Usa el feed de datos de TV (no Binance mainnet) → velas levemente distintas.
- Modela fills, fees y slippage distinto a `scripts/backtest_price_action.py`.
- No hace walk-forward: te muestra **in-sample** sobre el rango visible, que es
  justo el sesgo que el WFA (`scripts/audit_wfa_pa.py`) corrige.

**Conclusión:** usalo para *confirmar visualmente* que la lógica entra donde
esperás (sweeps reales, a favor de estructura), **no** para decidir si la
estrategia es rentable — para eso ya está el WFA en Python.

## Diferencias conocidas (chicas)

- TV puede tener micro-diferencias de timing en los pivots del 4h por cómo
  alinea las velas HTF; el bot calcula el 4h sobre datos de Binance.
- El cap de sizing por leverage es una aproximación del
  `Sim._position_size_usdt` (que también compara contra `trade_reserve_pct`).
