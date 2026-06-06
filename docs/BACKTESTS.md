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
