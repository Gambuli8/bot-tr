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

## 2026-06-05 — TP escalado + breakeven shift (roadmap #1)

Implementado el TP escalado: al tocar **TP1** (a `TP1_R_MULTIPLE` × la distancia
del SL, default R:R 1:1) se cierra `TP1_SIZE_PCT` de la posición (default 50%) y
el SL se mueve a **breakeven**. El remanente corre al TP completo (o al trailing).
La idea: asegurar ganancia parcial y convertir la segunda mitad en "trade gratis".

Implementado en los 3 lugares y detrás de flags (off por default):
- `config/settings.py`: `SCALED_TP`, `TP1_R_MULTIPLE`, `TP1_SIZE_PCT`, `BREAKEVEN_AFTER_TP1`.
- `scripts/backtest.py`: flag `--scaled-tp` + parcial en `_take_partial_tp1`.
- `execution/order_manager.py`: `maybe_take_partial_tp1()` + breakeven, contabiliza
  el parcial en `close_position`. Notificación Telegram `notify_partial_tp`.

### ⚠️ Validación con data real: PENDIENTE

El backtest 30d **no se pudo correr en el entorno remoto**: la política de red
bloquea el acceso a Binance (y a toda fuente de OHLCV) con `403`. Por eso **NO se
activó en producción** — `SCALED_TP=false` por default, el bot vivo no cambia.

Lo que SÍ se validó acá (determinístico, sin red):
- **Mecánica** (9 tests en `tests/test_scaled_tp.py`, simulador + OrderManager):
  parcial cierra la fracción correcta, breakeven mueve el SL al entry, el PnL total
  del trade = parcial + remanente, y el "trade gratis" cierra en verde aunque el
  precio vuelva al entry. Espejo LONG/SHORT cubierto.
- **Integración** del pipeline del backtester con data sintética: corre de punta a
  punta en ambos modos sin errores y los parciales se toman como se espera.

### Para validar con data real (correr en tu máquina, con acceso a Binance):

```bash
# 1) Baseline (config viva actual)
python scripts/backtest.py --days 30 --timeframe 15m --adx-min 20 --kelly --mtf

# 2) Con TP escalado
python scripts/backtest.py --days 30 --timeframe 15m --adx-min 20 --kelly --mtf --scaled-tp
```

Criterio de aceptación (regla dura del proyecto):
- Si **PF sube o se mantiene** y el **max DD no sube** → aplicar (`SCALED_TP=true` en `.env`).
- Si baja el PF o sube el DD → descartar, o probar variantes:
  `TP1_R_MULTIPLE` 1.5 (TP1 más lejos, menos parciales prematuros) y/o
  `TP1_SIZE_PCT` 0.33 (asegurar menos, dejar correr más).

> Nota de diseño: el baseline ya corre con trailing legacy activo
> (`trailing_stop_enabled=True`, activación 2%), así que el TP escalado se mide
> *encima* de eso. Ojo con la interacción breakeven vs trailing: ambos sólo
> endurecen el SL, no hay conflicto, pero el breakeven temprano puede aumentar
> las salidas a breakeven (menos ganadoras grandes). Eso es justo lo que el
> backtest tiene que medir.

---

## 2026-06-05 — Arquitectura Multi-Symbol (pool compartido + candado global)

Refactor de single-asset (BTC) a multi-symbol: escanea/opera una lista de pares
sobre un capital compartido, con candado de exposición global. Implementado en
`settings`, `exchange` (filtros dinámicos), `order_manager` (estado por símbolo +
candado + sizing del pool) y `main_strategy` (scanner secuencial). Detrás de
`SYMBOLS`/`MAX_CONCURRENT_TRADES`; con un solo símbolo es 100% retrocompatible.

### Validación
- **No toca la lógica de señal por símbolo** (mismo TechnicalEngine/SL/TP), sólo
  la orquestación y la gestión de capital. El backtest por símbolo sigue siendo
  válido para medir el edge de cada moneda (correr `scripts/backtest.py --symbol`).
- **31 tests nuevos/actualizados** (`tests/test_multisymbol.py` + ajustes):
  candado global, una posición por símbolo, independencia open/close, sizing del
  pool compartido (cabe N dentro de la reserva), daily-DD sobre equity, y
  migración del `state.json` viejo. Suite completa: 47 passed.
- Pendiente: **backtest de PORTAFOLIO** (simular las 5 monedas en paralelo con el
  candado y el pool compartido) para medir correlación/exposición real. Es un
  follow-up — el simulador actual es de un símbolo por corrida.

> Nota de sizing: con riesgo 2.5% y stops ~2%, el notional risk-based (>100% del
> equity) siempre topea contra la reserva. Para que entren 2 trades, cada uno se
> limita a `tradeable/2` (~73.5 USDT sobre 210). El riesgo real por trade queda
> por debajo del 2.5% nominal cuando el stop es ajustado — igual que en el bot
> single-symbol previo.

## Próximos experimentos pendientes

- [ ] **TP escalado: validar con data real 30d** (baseline vs `--scaled-tp`) ← listo para correr
- [ ] Trailing dinámico recalibrado (activar al 2:1, distance min 1%)
- [ ] Breakout por cierre confirmado (no intra-vela)
- [ ] SL estructural en niveles Donchian
- [ ] Comisiones simuladas en el backtest (0.15% round-trip)
- [ ] Walk-forward analysis (training 20d, testing 10d, rolling)
