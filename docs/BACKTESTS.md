# Historial de backtests

> Cada entrada documenta una corrida del `scripts/backtest.py` con sus flags
> y resultados. Agregar al final, NO sobrescribir, para tener historial real.

## Plantilla

```markdown
### YYYY-MM-DD — descripción corta
Fecha datos: rango  •  Símbolo: BTC/USDT  •  TF: 15m  •  Días: 30

Comando:
    python scripts/backtest.py --days 30 --timeframe 15m [flags]

Resultado:
- Capital: $X → $Y (+Z%)
- WR: %  •  PF: X  •  Max DD: %
- Trades: N (L LONG, S SHORT)
- Cierres: SL N / TP N

Conclusión: aplicada / rechazada / pendiente, razón.
```

---

## 2026-06-04 — Sesión inicial

Datos: `2026-05-05 → 2026-06-04` (30 días)  •  Símbolo: BTC/USDT  •  TF: 15m

### A) Baseline (sin mejoras)
```
python scripts/backtest.py --days 30 --timeframe 15m
```
- Capital: $200 → $217.72 (+8.86%)
- WR: 38.6%  •  PF: 1.56  •  Max DD: 3.43%
- Trades: 57 (26L / 31S)
- Cierres: SL 39 / TP 17

**Referencia.** Es lo que da el bot con la configuración inicial.

### B) Cooldown 5 velas
```
... --cooldown 5
```
- Capital: $200 → $203.42 (+1.71%)
- WR: 32.1%  •  PF: 1.09  •  Max DD: 5.09%
- Trades: 53 (18L / 35S)

❌ **Rechazado.** Empeora todo: filtra entradas válidas que llegaban inmediatamente después de un cierre por señal opuesta.

### C) Filtro ADX ≥ 20
```
... --adx-min 20
```
- Capital: $200 → $219.39 (+9.70%)
- WR: **44.4%**  •  PF: **1.79**  •  Max DD: 3.43%
- Trades: 45 (17L / 28S)
- Cierres: SL 28 / TP 16

✅ **Aplicada.** WR +5.8pp, PF +0.23, mismo DD. Menos trades = menos comisiones en real.

### D) Confirmación macro EMA200
```
... --macro-trend
```
- Capital: $200 → $216.57 (+8.28%)
- WR: 32.7%  •  PF: 1.55  •  Max DD: 3.36%

➖ **Neutro.** No aporta vs baseline. Descartado.

### E) Kelly fraccionado
```
... --kelly
```
- Capital: $200 → $217.81 (+8.90%)
- WR: 38.6%  •  PF: 1.56

➕ **Marginal positivo.** Adoptado por consistencia teórica (sizing dinámico) y por escalado seguro.

### F) ADX + Kelly combinados
```
... --adx-min 20 --kelly
```
- Capital: $200 → $219.46 (+9.73%)
- WR: 44.4%  •  PF: 1.79  •  Max DD: 3.43%

✅ Confirma que ADX y Kelly son ortogonales.

### G) ADX sweep (con Kelly fijo)
| ADX min | Retorno | WR | PF | Max DD |
|---|---|---|---|---|
| 15 | +6.94% | 35.7% | 1.43 | 5.26% |
| 18 | +7.41% | 39.2% | 1.52 | 3.49% |
| **20** | **+9.70%** | **44.4%** | **1.79** | 3.43% |
| 22 | +4.86% | 38.8% | 1.36 | 3.43% |
| 25 | +2.16% | 35.6% | 1.17 | 3.60% |
| 30 | +6.51% | 43.8% | 1.80 | 2.49% |

20 es el óptimo claro en este dataset. 30 da PF similar pero opera menos.

### H) MTF Confluence (1h) — agregada sobre ADX+Kelly
```
... --adx-min 20 --kelly --mtf
```
- Capital: $200 → **$224.94** (+12.47%)
- WR: **47.8%**  •  PF: **2.30**  •  Max DD: **2.47%**
- Trades: 46 (8L / 38S)

✅ **APLICADA.** Mejor entrada hasta hoy:
- Retorno +2.35pp absoluto vs ADX+Kelly (+23% relativo)
- PF +0.46 (+25%)
- Max DD baja casi 1pp
- Composición cambia drásticamente: solo 8 LONGs en 30d con mercado bearish — el MTF filtra los longs contra macro tendencia.

### I) Trailing dinámico (activado al 1:1)
```
... --adx-min 20 --kelly --mtf --dyn-trailing
```
- Capital: $200 → $200.03 (+0.01%)
- WR: 48.4%  •  PF: **1.00**  •  Max DD: 3.90%
- Cierres: SL 62 / TP 0

❌ **Rechazado.** El trailing activa demasiado temprano y mata las ganancias antes de que crezcan. Todas las salidas son por stop-loss. Necesita recalibración (activar al 2:1, distance ≥ 1% o 3×ATR).

---

## Configuración aplicada al bot vivo

Tras esta sesión, el `.env` quedó con:
```
ADX_MIN_TRENDING=20
USE_KELLY_SIZING=true
REQUIRE_MTF_CONFLUENCE=true
```

Esperado en producción (30 días):
- Retorno: ~+12% (sin comisiones)
- WR: ~48%
- PF: ~2.3
- Max DD: ~2.5%

⚠️ El backtest **no incluye comisiones** (0.075% por lado en taker = 0.15% round-trip). Con $140 por trade × 46 trades = ~$10 en fees en 30 días = ~5% del capital. **El resultado real esperado es ~+7% en 30 días con comisiones**, no +12.47%.

## Próximos experimentos pendientes

- [ ] Trailing dinámico recalibrado (activar al 2:1, distance min 1%)
- [ ] Breakout por cierre confirmado (no intra-vela)
- [ ] SL estructural en niveles Donchian
- [ ] Comisiones simuladas en el backtest (0.15% round-trip)
- [ ] Walk-forward analysis (training 20d, testing 10d, rolling)

---

## 2026-06-06 — TP escalado

Datos: `2026-05-07 → 2026-06-06` (30 días)  •  Símbolo: BTC/USDT  •  TF: 15m

### J) Baseline actualizado (ADX + Kelly + MTF, mismo período)
```
python scripts/backtest.py --days 30 --timeframe 15m --adx-min 20 --kelly --mtf
```
- Capital: $200 → $231.57 (+10.27%)
- WR: 46.0%  •  PF: 1.93  •  Max DD: 2.40%
- Trades: 50 (6L / 44S)

### K) Sweep de TP escalado
Comando: `... --tp-scaling --tp1-rr <r> --tp1-pct <p>`

| TP1 ratio | Frac cerrada | Retorno | WR | PF | Max DD |
|---|---|---|---|---|---|
| 0.7 | 0.3 | +3.48% | 77.8% | 1.29 | 3.08% |
| 0.7 | 0.5 | +3.69% | 77.8% | 1.31 | 2.66% |
| 0.7 | 0.7 | +3.89% | 77.8% | 1.33 | 2.29% |
| 1.0 | 0.3 | +8.05% | 76.9% | 1.90 | 1.95% |
| 1.0 | 0.5 | +8.14% | 76.9% | 1.91 | 1.89% |
| 1.0 | 0.7 | +8.22% | 76.9% | 1.92 | 1.89% |
| **1.3** | **0.3** | **+10.47%** | **70.7%** | **2.14** | **2.11%** |
| 1.3 | 0.5 | +10.35% | 70.7% | 2.13 | 2.15% |
| 1.3 | 0.7 | +10.21% | 70.7% | 2.11 | 2.20% |
| 1.5 | 0.3 | +9.33% | 68.8% | 1.91 | 2.09% |
| 1.5 | 0.5 | +9.22% | 68.8% | 1.90 | 2.12% |
| 1.5 | 0.7 | +9.02% | 68.8% | 1.88 | 2.15% |

### L) TP1 a 1.3× SL, cerrar 30% — DESCARTADO tras agregar comisiones
Sin fees parecía ganador. Pero al modelar fee 0.1% por lado, perdió contra el baseline:

| Config (con fee 0.1%) | Retorno | WR | PF | DD |
|---|---|---|---|---|
| Baseline (ADX+Kelly+MTF) | +4.66% | 46.9% | **1.33** | 3.97% |
| TP escalado 1.3×, 0.3 | +2.59% | 59.8% | 1.20 | 4.80% |
| TP escalado 1.5×, 0.3 | +1.66% | 58.8% | 1.11 | 4.40% |
| TP escalado 2.0×, 0.5 | -0.21% | 57.1% | 0.98 | 5.50% |
| TP escalado 2.5×, 0.3 | +2.57% | 56.2% | 1.17 | 4.11% |

❌ **REVERTIDO.** El fill extra de TP1 cuesta ~0.15% del capital invertido cada vez. La ganancia parcial chica del TP1 no compensa esa fricción.

---

## 2026-06-06 — Comisiones reales

Agregamos `commission_pct_per_side=0.001` (Binance Spot taker sin BNB) al simulador.

### M) Re-evaluación de todo el stack con comisiones reales

| Variante (30d) | Sin fees | Con fees 0.1%/side | Δ retorno |
|---|---|---|---|
| Sin filtros (baseline original) | +8.86% | n/d | — |
| ADX≥20 + Kelly + MTF | +11.96% | **+4.66%** | **-7.30pp** |
| ADX≥20 + Kelly + MTF + TP escalado | +10.47% | +2.59% | -7.88pp |

**Lectura crítica**:
- Las comisiones se llevan **más del 60% del retorno bruto** sobre 30 días.
- El bot real va a dar **~+5% / mes** (sin gente que pague comisiones reducidas con BNB).
- El PF cae de 2.14 a 1.33 — sigue siendo positivo pero el margen es chico.
- TP escalado, contrintuitivo, EMPEORA con fees por el fill extra.

**Acción tomada**:
- TP escalado: revertido (`TP_SCALING_ENABLED=false`)
- Config activa del bot: ADX≥20 + Kelly + MTF
- Expectativa realista comunicada al usuario: ~+5% mensual neto.

---

## 2026-06-07 — Fase 1 Roadmap: Auditoría Estadística del ScalpingEngine

Antes de migrar el ScalpingEngine a Binance real (testnet=false), corrimos
las primeras auditorías cuantitativas del roadmap. **Hallazgo crítico**: el
motor no tiene edge en el agregado, pero hay edge LATENTE en sub-regímenes.

### Aud #1 — Monte Carlo / Bootstrap del orden de trades

`scripts/audit_montecarlo.py` — 1000 permutaciones del orden de PnLs, 60d
BTC/USDT 5m, risk 8%, leverage 10×, fees maker 0.02% / taker 0.05%.

| Métrica | Valor |
|---|---|
| Trades reales | 64 (luego 62 con datos actualizados) |
| WR real | 50.0–51.6% |
| Retorno real | **−2.36%** (corrida MC) / +0.96% (corrida posterior) |
| Max DD real observado | 12.89% |
| P(retorno < 0) | **100%** (suma de PnLs es invariante bajo shuffle) |
| P(max_dd > 12.89%) | 75.9% — el orden real fue afortunado |
| Distribución de DD (mediana / P95) | 15.07% / 21.91% |

**Veredicto**: ❌ edge no robusto. El PnL agregado es ~0 con std grande;
cualquier orden razonable produce retornos negativos o nulos. Esperanza
matemática del motor en su configuración actual ≈ 0.

### Aud E — Disección por sub-regímenes

`scripts/audit_trade_breakdown.py` — separa los 62 trades por hora UTC, día
de la semana, lado, régimen de ATR%, BB width al entrar, intensidad del
volume spike y duración. Significancia con t-stat aproximado (n≥10, |t|>1.5).

**Buckets con edge significativo:**

| Bucket | n | WR | avg PnL | t-stat |
|---|---|---|---|---|
| ATR% Q2 (0.15–0.18%) | 16 | 75.0% | **+$1.46** | +1.97 ✅ |
| BB width Q2 (0.0049–0.0066) | 16 | 68.8% | **+$1.22** | +1.52 ✅ |
| Hora 00–06 UTC (Asia) | 18 | 66.7% | +$1.32 | +1.38 🟡 |
| Volume spike Q1 (≤2.38×) | 16 | 62.5% | +$1.41 | +1.28 🟡 |

**Buckets perdedores (el ladrón del PnL):**

| Bucket | n | WR | avg PnL | total |
|---|---|---|---|---|
| Hora 06–12 UTC (EU AM) | 12 | 33.3% | −$1.55 | **−$18.55** |
| BB width Q3 (zona media) | 15 | 33.3% | −$1.53 | −$22.87 |
| Domingo | 8 | 37.5% | −$1.56 | −$12.50 |
| Viernes | 8 | 37.5% | −$0.80 | −$6.38 |

**Lectura**:
- El edge vive en **volatilidad media** (ATR ~0.15–0.18%, BB width ~0.005–0.007).
  Volatilidad muy baja → fees comen el TP. Volatilidad muy alta → SL random
  te liquida más rápido que el TP llega.
- **EU AM (06–12 UTC) destruye el bot** — hipótesis: HFT EU activo + news
  flow europeo.
- Asia (00–06 UTC) es la sesión rentable: cripto fluye más limpio.
- Spikes de volumen MUY grandes (>4×) son explosiones de noise que revierten.

**Caveat metodológico**: 28 buckets × α=0.05 esperaría ~1.4 ✅ por puro ruido;
tenemos 2. No es prueba, pero los hallazgos son **coherentes temáticamente**.
La prueba real es WFA.

### Aud F — Filtros incrementales del breakdown

`scripts/audit_filters.py` — toma los buckets perdedores y los aplica como
filtros pre-trade, midiendo el efecto incremental sobre el agregado del
backtest 60d.

| Filtro | n | WR | avg PnL | total | ret 60d | DD | PF |
|---|---|---|---|---|---|---|---|
| Sin filtro (baseline) | 62 | 51.6% | +$0.03 | +$2.01 | +0.96% | 12.89% | 1.02 |
| **A: −EU AM (06-12 UTC)** | **50** | **56.0%** | **+$0.41** | **+$20.56** | **+9.79%** | **11.35%** | **1.23** ✅ |
| B: A + ATR%∈[0.13, 0.20] | 30 | 56.7% | +$0.21 | +$6.34 | +3.02% | 6.84% | 1.15 |
| C: B + vol_ratio≤4× | 22 | 50.0% | −$0.22 | −$4.92 | −2.34% | 6.99% | 0.86 |
| D: C + −Dom/Vie | 17 | 47.1% | −$0.39 | −$6.59 | −3.14% | 8.19% | 0.78 |

**Hallazgo principal**: el filtro A (excluir señales en hora 06-12 UTC)
**multiplica el retorno por 10× con DD apenas mejor**. Los filtros B/C/D
**empeoran** progresivamente — son overfit del mismo dataset que los derivó.
Conclusión: **un solo filtro de horario es el ganador. Keep it simple.**

**Mecanismo razonable**: la sesión EU AM (Londres open) en BTC 5m está
dominada por algos de HFT + news flow europeo que matan las señales de
breakout BB con whipsaws. La sesión Asia (00-06 UTC) y las sesiones
calmas (12-24 UTC) tienen flow más limpio para una estrategia de
volatility expansion.

**Optimización del engine durante esta auditoría**: `core/scalping_engine.py`
ahora cachea BB+ATR por `id(df)`. El `analyze()` pasa de O(n²) a O(n) en
backtests largos. Crítico para que el WFA (#4) sea viable.

### Aud #4 — Walk-Forward Analysis (180d BTC/USDT 5m, filtro A activo)

`scripts/audit_wfa.py --days 180 --is-days 40 --os-days 20 --step-days 20 --grid quick`

Grid de 3 configs (squeeze_pct, vol_spike, tp_atr_mult, cooldown), 7 ventanas
rolling IS/OS. En cada ventana se elige la mejor config IS por PF y se mide
su performance en el OS siguiente (no overlap).

| IS→OS (OS range) | Best cfg IS | IS ret | **OS ret** | OS PF | OS n |
|---|---|---|---|---|---|
| 01-18 → 02-07 | sq=15/vs=2.5/tp=2.0/cd=5 | −14.69% | **+13.70%** | 1.77 | 18 |
| 02-07 → 02-27 | sq=15/vs=2.5/tp=2.0/cd=5 | +13.66% | −7.63% | 0.74 | 25 |
| 02-27 → 03-19 | sq=15/vs=2.5/tp=2.0/cd=5 | +5.03% | −13.34% | 0.61 | 23 |
| 03-19 → 04-08 | sq=15/vs=2.5/tp=2.0/cd=5 | −21.44% | −17.90% | 0.35 | 20 |
| 04-08 → 04-28 | sq=25/vs=1.5/tp=1.5/cd=3 | −47.31% | −1.57% | 0.96 | 39 |
| 04-28 → 05-18 | sq=25/vs=1.5/tp=1.5/cd=3 | −23.18% | −0.73% | 0.94 | 14 |
| 05-18 → 06-07 | sq=20/vs=2.0/tp=1.5/cd=3 | +0.05% | **+9.74%** | 1.54 | 20 |

**Resumen agregado**:
- Ventanas OS > 0: **2/7 (29%)**
- OS retorno total: **−17.74%**
- OS retorno medio: −2.53% por ventana
- OS retorno peor: −17.90% — OS retorno mejor: +13.70%

**Veredicto**: ❌ **EDGE NO SOBREVIVE WFA**. El +9.79% del audit F era
cherry-picking temporal (corresponde sólo a la ventana 05-18→06-07). La
config "ganadora IS" cambia entre ventanas — no hay parámetros estables.
La correlación IS↔OS es ~ruido (ventana 1 inversa: IS −15% / OS +14%).

**Lectura honesta**: el ScalpingEngine BB-squeeze en BTC/USDT 5m **no tiene
edge robusto** con fees reales (maker 0.02% + taker 0.05%), risk 8%,
leverage 10×. El filtro de horario era ruido reciente, no señal estructural.

### Decisión final de la Fase 1

**NO migrar a producción real con ScalpingEngine BTC 5m.** El roadmap
detectó el problema antes de poner capital real ($210). Auditorías #2
(slippage / Post-Only) y #3 (Kelly) **canceladas** — son polish sobre un
motor sin esperanza matemática positiva. Kelly sobre EV negativo da
fracción negativa = no operar.

**Pivot propuesto (Fase 2)**:
- A. Re-correr el WFA con scalping en **TF 15m** (menos noise, menos fees por capital).
- C. Re-validar el **PriceActionEngine** sobre los mismos 180d (era el otro motor
  validado en backtests anteriores, antes deprecadi por baja frecuencia).
- Comparar A vs C en igual de condiciones (período, fees, leverage, risk).

---

## 2026-06-07 — Fase 2: Pivot al PriceActionEngine + escalamiento horizontal

### Decisión preliminar

Se descarta el ScalpingEngine en cualquier TF tras WFA fallido. Se reactiva
el PriceActionEngine (validado en backtests previos pero con frecuencia baja).
Objetivo: alcanzar ~1 trade/día sumando múltiples activos (escalamiento
horizontal) sin tocar el riesgo por trade.

### Baseline PA — BTC 1h 180d (single backtest, no WFA)

`scripts/backtest_price_action.py --days 180`

| Métrica | Valor |
|---|---|
| Trades | 21 (0.82/semana) |
| WR | 47.6% |
| PF Neto | **1.66** |
| Retorno | +12.72% |
| Max DD | 6.51% |
| Liquidaciones | 0 |
| Risk/trade | 2.5% (conservador) |
| Leverage | 1× (Spot) |

**Edge confirmado en single backtest. La frecuencia baja (0.82/sem) sigue
siendo el problema operativo a resolver via multi-asset.**

### Aud — WFA PA en 3 timeframes (Camino 1: bajar TF)

`scripts/audit_wfa_pa.py` con grid quick (3 configs), 7 ventanas IS=40d/OS=20d,
180d totales, risk 8%, leverage 5×, fee 0.05%, sobre BTC/ETH/SOL @ 15m / 30m / 1h.

**Resultado del TF sweep**:

| Sym \ TF | 1h | 30m | 15m |
|---|---|---|---|
| BTC | ✅ 5/7, **+25.05%** | 🟡 3/7, +24.60% | (abortado) |
| ETH | 🟡 2/7, +3.12% | ❌ 2/7, −14.71% | (abortado) |
| SOL | ✅ 5/7, **+38.46%** | ❌ 2/7, **−57.49%** | (abortado) |

**Conclusión TF sweep**:
- BTC es el ÚNICO estable across TFs (+25% vs +24% — mantiene retorno)
- SOL colapsa al bajar TF: +38% en 1h → −57% en 30m (overfit grosero)
- ETH es estructuralmente flojo en cualquier TF
- 15m: abortados; el patrón sugería que iban a colapsar más
- **Decisión**: descartar 15m y 30m. PA solo en 1h.

### Aud — WFA PA multi-asset 1h (Camino 2: escalamiento horizontal)

Se corrió el WFA en 1h sobre 12 activos top-liquidez de Binance, mismos
parámetros (180d, IS=40d/OS=20d, risk 8%, leverage 5×, fee 0.05%, grid quick).

| Coin | OS pos | OS total | OS medio | Trades OS | t/día | Decisión |
|---|---|---|---|---|---|---|
| **BTC** | **5/7** (71%) | **+25.05%** | +3.58% | 18 | 0.13 | ✅ APRUEBA (núcleo) |
| **SOL** | **5/7** (71%) | **+38.46%** | +5.49% | 17 | 0.12 | ✅ APRUEBA (núcleo) |
| **AVAX** | 4/7 (57%) | +8.46% | +1.21% | 22 | 0.16 | 🟡 APRUEBA (borde +) |
| **LINK** | 4/7 (57%) | +6.13% | +0.88% | 16 | 0.11 | 🟡 APRUEBA (borde +) |
| ETH | 2/7 | +3.12% | +0.45% | 15 | 0.11 | ❌ MARGINAL |
| LTC | 4/7 | **−21.30%** | −3.04% | 22 | 0.16 | ❌ (script ✅ pero perdió) |
| ADA | 3/7 | −5.91% | −0.84% | 21 | 0.15 | ❌ |
| DOT | 3/7 | −12.45% | −1.78% | 15 | 0.11 | ❌ |
| BNB | 1/7 | −33.01% | −4.72% | 25 | 0.18 | ❌ |
| INJ | 2/7 | −30.32% | −4.33% | 20 | 0.14 | ❌ |
| DOGE | 1/7 | −13.06% | −1.87% | 24 | 0.17 | ❌ |
| XRP | 1/6 | −11.96% | −1.99% | 11 | 0.09 | ❌ |
| POL | 2/7 | +56.60% | +8.09% | 19 | 0.14 | ❌ cherry-pick 1 ventana de +88% |

### 🐛 Bug detectado: PF medio inflado por ventanas con n<3

El script declaraba LTC y LINK como "✅ EDGE ROBUSTO" porque su **PF medio
calculado era >14**. Pero ese promedio estaba contaminado por ventanas OS
con n=1 o n=2 trades donde el PF resultaba `inf` (zero losses) — el código
reemplazaba `inf` por 99.0 y lo promediaba, distorsionando el resultado.

**Caso concreto LTC**: 7 ventanas OS con PFs `[2.09, 0.00, 0.56, 99.00,
1.28, 1.18, 0.00]`. Media = 14.87 (parece edge robusto). **Mediana del PF
filtrando ventanas con n<3 = 1.18** (refleja la realidad: dos veces a la
par, una vez con edge marginal). OS retorno total: −21.30% (perdedor).

**Fix aplicado** en `scripts/audit_wfa_pa.py`:
1. Reportar **mediana del PF**, no media (resistente a outliers).
2. Filtrar ventanas con `n<3` del cálculo del PF (muestras chicas).
3. Veredicto pide ahora **PF mediano ≥ 1.15 AND OS positivas ≥ 55% AND
   OS retorno total ≥ +10%**. El tercer criterio mata el caso LTC.

### Decisión final: portafolio aprobado

**Opción B (Portafolio Ampliado)** — 4 activos aprobados para producción:

| Activo | OS positivas | OS retorno 180d | Trades OS | t/día |
|---|---|---|---|---|
| BTC/USDT | 5/7 (71%) | +25.05% | 18 | 0.13 |
| SOL/USDT | 5/7 (71%) | +38.46% | 17 | 0.12 |
| AVAX/USDT | 4/7 (57%) | +8.46% | 22 | 0.16 |
| LINK/USDT | 4/7 (57%) | +6.13% | 16 | 0.11 |
| **Portafolio** | — | **+78.10%** (suma simple) | **73** | **0.52** |

**Frecuencia agregada esperada**: ~3.6 trades/semana, ~15/mes. No
alcanzamos 1 trade/día pero el cliente aprobó priorizar calidad y
protección de capital (capital nominal $210 + inyecciones mensuales $200).

**Hallazgos de calidad — diseño del portafolio**:
1. BTC + SOL son el núcleo de edge demostrado (5/7 ventanas positivas,
   retornos sólidos de doble dígito).
2. AVAX + LINK son borde aceptable: 4/7 ventanas positivas con OS total
   levemente positivo y n>15 (muestra suficiente).
3. ETH y LTC fueron explícitamente descartados pese a confundir al script
   inicial.
4. POL fue descartado por cherry-picking: una sola ventana de +88% inflaba
   un resultado que era −31.56% en las restantes 6 ventanas.

Las instrucciones para desplegar este portafolio en el VPS están en
`docs/DEPLOYMENT_MULTI_ASSET.md`.

---

## 2026-06-08 — Fase 3: Pivot a Binance Futures USDT-M con leverage 7×

### Motivación

Durante la validación en Spot 1× (Fase 2) detectamos al correr el Pine en
TradingView que TANTO el WFA Python COMO el Pine usaban **leverage 5×** —
pero el bot real en VPS estaba en **Spot 1×** (`defaultType: spot` en
`core/exchange.py`). Implicaba que los retornos esperados en producción
serían ~1/5 de los del WFA (BTC: +5% en 180d vs el +25% del WFA).

El cliente decidió migrar a **Futures USDT-M con leverage 7×** para alinear
operación con el WFA, aceptando los riesgos (liquidación, funding rates).

### Aud — WFA comparativo de leverage (5× vs 7×) sobre los 4 pares Fase 2

Re-corrido del `scripts/audit_wfa_pa.py` con los mismos pares aprobados en
Fase 2 (BTC, SOL, AVAX, LINK), 180d, 7 ventanas IS=40d/OS=20d, risk 8%,
fees 0.05%. Una corrida por cada par × leverage.

| Coin | 5× OS pos / OS total / PF med / veredicto | 7× OS pos / OS total / PF med / veredicto |
|---|---|---|
| **BTC** | 5/7 / **+15.16%** / 1.22 / ✅ | 5/7 / **+18.73%** / 1.18 / ✅ |
| **SOL** | 4/7 / +6.41% / 1.11 / 🟡 | 4/7 / **+11.95%** / 1.68 / ✅ |
| **AVAX** | 5/7 / +4.56% / 1.28 / 🟡 | 5/7 / **+14.15%** / 1.56 / ✅ |
| **LINK** | 3/7 / +9.51% / 0.93 / ❌ | 3/7 / +6.37% / 0.97 / ❌ |

**Observaciones**:
- BTC ya pasaba con 5×, pero 7× sube +3.5pp el retorno OS.
- **SOL y AVAX no pasan con 5×** (PF medio OK pero OS total < +10% threshold).
  Con 7× la amplificación del edge los lleva al ✅: +12% y +14% respectivamente.
- **LINK no pasa con ningún leverage** y se descarta del portafolio.
  El edge se diluyó completamente: PF mediano <1 indica que en términos de
  retornos por trade, las pérdidas pesan más que las ganancias.
- Frecuencia agregada no cambia con leverage (es 0.39 trades/día con los 3
  que sí pasan, contra 0.52 si se hubieran mantenido los 4).
- Nota metodológica: estos números difieren levemente del WFA Fase 2 porque
  Binance entregó 1 día extra de data — los OS más recientes cambiaron.

### 10× evaluado y descartado cualitativamente

Antes de confirmar 7×, se consideró probar 10×. Sin correr el WFA se
descartó porque:
- 10× liquida con caída ~9% del precio.
- AVAX y SOL pueden moverse 9-15% en 1 vela de 1h en períodos de stress.
- Probabilidad de al menos 1 liquidación en 6 meses es alta para alts.
- El sim del WFA no modela perfectamente la liquidación a leverage alto,
  por lo que el resultado del backtest sería optimista.

### Decisión final Fase 3

**Portafolio aprobado para producción real**: BTC + SOL + AVAX en
**Futures USDT-M, leverage 7×, isolated margin, risk 5% por trade**.

| Activo | OS retorno 180d (7×) | OS positivas | PF mediano | Trades OS | t/día |
|---|---|---|---|---|---|
| BTC/USDT | +18.73% | 5/7 (71%) | 1.18 | 18 | 0.13 |
| SOL/USDT | +11.95% | 4/7 (57%) | 1.68 | 17 | 0.12 |
| AVAX/USDT | +14.15% | 5/7 (71%) | 1.56 | 20 | 0.14 |
| **Portafolio** | **+44.83%** | — | — | **55** | **~0.39** |

**Cambios operativos vs Fase 2**:
- De 4 bots → 3 bots (sin LINK).
- Capital nominal por bot: $52.5 → **$70** ($210 / 3).
- Risk per trade: 8% → **5%** (margen vs liquidación con leverage 7×).
- Mercado: Spot → Futures USDT-M.
- Margin mode: N/A → **isolated** (sin contagio entre trades).
- Stop loss exchange order: `STOP_LOSS` → `STOP_MARKET` con `reduceOnly`.
- Take profit exchange order: `LIMIT` (Spot) → `LIMIT` + `reduceOnly`.
- API keys: hay que generar **nuevas keys de Futures Trading**, las de Spot
  no sirven.
- Testnet URL: `testnet.binance.vision` → **`testnet.binancefuture.com`**.

### Refactor de código (commit XXX)

- `core/exchange.py`: `defaultType: future`, agregado `configure_leverage_and_margin()`
  (idempotente, llamado antes de cada `place_market_order`), `STOP_MARKET` +
  `reduceOnly` para SL, `LIMIT` + `reduceOnly` para TP, log con `base_asset`
  derivado del símbolo (no más "BTC" hardcoded en place_market_order).
- `config/settings.py`: agregado `leverage: int = 7` y `margin_mode: str = "isolated"`.
- `.env.example`: nueva sección Futures + comentarios actualizados.
- `docker-compose.multi.yml`: removido `bot-link`.
- `docs/DEPLOYMENT_MULTI_ASSET.md`: instrucciones completamente actualizadas
  para testnet Futures + generación de keys Futures.

### Próximos pasos

1. **Validación 7 días en testnet Futures** (testnet.binancefuture.com).
2. Si pasa los criterios → cambiar `BINANCE_TESTNET=false` con keys de
   Futures Mainnet.
3. **Pendiente para próxima iteración**: reconciliación robusta de
   `fetch_positions` (hoy reconcilia solo vía órdenes — funciona pero
   no detecta liquidaciones inmediatamente).

---

## 2026-06-10 — Nuevo motor: PullbackScalpEngine (trend-pullback, fee-gated)

### Motivación

El cliente pidió un bot de scalping intradía "que no falle y que las comisiones
no coman el profit". El `ScalpingEngine` v1 (BB-squeeze breakout) ya había sido
descartado en Fase 1 por WFA (2/7 OS positivas, −17.74% OS, EV ≈ 0 con fees).

En vez de re-tunear el mismo motor sin edge, se diseñó uno **estructuralmente
distinto** que ataca cada falla documentada en la auditoría de v1:

| Falla documentada de v1 (BB-squeeze) | Cómo lo ataca el pullback engine |
|---|---|
| Breakout sin edge direccional (~50% WR) | Sólo opera A FAVOR de tendencia (EMA fast/slow + ADX≥18) |
| Breakout obliga a entrar taker (perseguir precio) | Entra en el pullback → fill Limit Post-Only = **maker** |
| Vol muy baja (BTC ATR~0.15%) → fees comen TP | Gate de EV neto + piso TP≥6× fee + banda de ATR% |
| EU AM (06-12 UTC) whipsaw | `skip_hours_utc` (mismo filtro de sesión) |
| Spikes vol >4× revierten | Guard: rechaza vela de reclaim si vol > 4× MA |

### Decisión de instrumento

**BTC es de los peores activos para scalpear**: ATR% intradía bajo (~0.15%) →
el fee fijo (% del notional) pesa demasiado. La comisión round-trip maker es
~0.04%, así que un TP de 0.26% deja el fee en ~15% del bruto (límite del gate).
Recomendación: **validar en SOL/USDT** (ATR% típicamente 2-3× BTC). Verificación
numérica del gate de fee (mismos fees maker 0.02% / taker 0.05%, WR conservador
55%):

| Setup | ATR% | TP bruto | Gate |
|---|---|---|---|
| BTC-like | 0.15% | 0.26% | ❌ REJECT (fee = 15% del bruto) |
| SOL-like | 0.45% | 0.79% | ✅ OK (EV neto +0.158%/trade, fee 5% del bruto) |
| Vol baja | — | 0.10% | ❌ REJECT (fee = 40% del bruto) |

→ El motor **rechaza por diseño** el setup que mató a v1, y sólo dispara cuando
el movimiento supera holgadamente la comisión.

### Estado: ⚠️ NO VALIDADO

Implementado: `core/pullback_scalp_engine.py`, settings `pbs_*`, wiring opt-in
en `main_strategy` (`ENGINE=pullback`, default sin cambiar), backtest
`scripts/backtest_pullback.py`. El backtest real NO se pudo correr en el entorno
de desarrollo (sin acceso de red a Binance/Kraken/Bybit/OKX). **Pasos obligatorios
antes de cualquier dinero real, en el VPS:**

```bash
# 1) Backtest single-period en SOL (y AVAX)
python scripts/backtest_pullback.py --symbol SOL/USDT --days 90
python scripts/backtest_pullback.py --symbol AVAX/USDT --days 90

# 2) Si PF neto > 1.3 y DD sano → WFA (adaptar audit_wfa.py al engine pullback)
# 3) Sólo si pasa WFA (≥5/7 OS positivas) → paper en testnet → real
```

Mientras no pase WFA, este motor queda **al mismo nivel que v1: hipótesis, no
producto**. La disciplina que mató a v1 antes de poner capital se mantiene.
