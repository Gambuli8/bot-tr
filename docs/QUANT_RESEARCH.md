# Research cuantitativo — BTC/USDT 4h spot

Generado: 2026-11-17 20:56 UTC · datos 2022-11-18 → 2026-11-17 (8760 velas, 0 reparadas) · Binance vía CCXT

Costos por lado: comisión 0.10% + slippage 0.05% (ida y vuelta ≈ 0.30%); funding 0.00%/8h (spot). Capital 1000 USDT, riesgo 1% por operación, kill-switch diario −3%, halt por drawdown −15%.

## Veredicto: ❌ NINGUNA estrategia pasa todos los filtros

Más cercana: **Rompimiento de volatilidad (Donchian)** (7/8 filtros).

## Fase 2 — backtest de muestra completa (parámetros por defecto, con costos)

| Estrategia | Neto | CAGR | Sharpe | Máx DD | Trades | Acierto | PF | Exposición | % grilla > 0 |
|---|---|---|---|---|---|---|---|---|---|
| Rompimiento de volatilidad (Donchian) | +43.1 % | +9.4 % | 1.13 | 14.0% | 81 | 33% | 1.78 | 31% | 100% |
| Cruce de tendencia EMA | +60.6 % | +12.6 % | 1.34 | 9.2% | 48 | 33% | 2.68 | 50% | 100% |
| Reversión a la media RSI | -7.4 % | -1.9 % | -0.60 | 12.0% | 100 | 63% | 0.74 | 10% | 17% |
| Comprar y mantener | +281.6 % | +39.8 % | 0.97 | 53.4% | 1 | – | – | 100% | – |

## Fase 3 — walk-forward (12 m entrenamiento / 3 m fuera de muestra) + Monte Carlo

| Filtro (fijado antes de correr) | Umbral | Rompimiento de volatilidad (Donchian) | Cruce de tendencia EMA | Reversión a la media RSI |
|---|---|---|---|---|
| Combinaciones de la grilla con neto > 0 | ≥ 60 % | ✅ 100.0% | ✅ 100.0% | ❌ 16.7% |
| Retorno neto OOS | > 0 | ✅ 17.4% | ✅ 14.9% | ✅ 0.1% |
| Profit factor OOS | ≥ 1,10 | ✅ 1.49 | ✅ 1.62 | ❌ 1.01 |
| Trades OOS | ≥ 25 | ✅ 70 | ✅ 34 | ✅ 51 |
| Eficiencia walk-forward (CAGR OOS / IS) | ≥ 0,30 | ✅ 0.37 | ❌ 0.25 | ❌ 0.03 |
| Monte Carlo: P(pérdida) | < 10 % | ❌ 11.6% | ❌ 22.5% | ❌ 47.6% |
| Monte Carlo: DD percentil 95 | < 25 % | ✅ 14.6% | ✅ 16.7% | ✅ 10.6% |
| OOS con costos ×2 | > 0 | ✅ 6.9% | ✅ 11.3% | ❌ -5.4% |

## Rompimiento de volatilidad (Donchian): detalle

**Edge en una oración:** BTC tiene momentum de mediano plazo: los rompimientos de máximos de 1–2 semanas tienden a extenderse, y cortar rápido las falsas rupturas deja pérdidas chicas y ganancias largas.

**Reglas exactas (parámetros elegidos con los últimos 12 meses):** **Entrada:** la vela de 4h CIERRA por encima del máximo de las 40 velas anteriores → compra a mercado en la apertura siguiente. **Stop inicial:** entrada − 3.0 × ATR(14). **Salida:** stop dinámico = mínimo de las últimas 30 velas (sólo sube; se ejecuta intrabar). Tamaño: 1 % del equity en riesgo hasta el stop inicial.

| Fuera de muestra (walk-forward) | Estrategia | Comprar y mantener |
|---|---|---|
| Período | 2023-11-18 → 2026-11-16 | idem |
| Beneficio neto | +17.4 % (+174 USDT) | +73.3 % (+733 USDT) |
| CAGR | +5.5 % | +20.1 % |
| Drawdown máximo | 9.5% | 53.4% |
| Sharpe | 0.76 | 0.63 |
| Trades / acierto / PF | 70 / 37% / 1.49 | – |
| t-stat del retorno medio por trade | 1.19 | – |
| Folds OOS positivos | 50% | – |

Retorno OOS por año: 2023: +2.4 % · 2024: +12.7 % · 2025: +0.4 % · 2026: +1.3 %

Monte Carlo (5000 remuestreos de los 70 trades OOS): P(pérdida) 11.6% · retorno p5 -5.4 % / mediana +17.1 % · DD mediana 7.7% / p95 14.6% · P(tocar el halt de −15%) 4.4%.

Parámetros elegidos en cada fold:

| OOS desde | hasta | Parámetros | Retorno OOS | Trades |
|---|---|---|---|---|
| 2023-11-18 | 2024-02-17 | `{'n_entry': 40, 'n_exit': 30, 'atr_mult': 2.0}` | +7.4 % | 6 |
| 2024-02-18 | 2024-05-19 | `{'n_entry': 40, 'n_exit': 20, 'atr_mult': 2.0}` | +2.5 % | 6 |
| 2024-05-19 | 2024-08-18 | `{'n_entry': 40, 'n_exit': 20, 'atr_mult': 2.0}` | -1.2 % | 5 |
| 2024-08-18 | 2024-11-17 | `{'n_entry': 20, 'n_exit': 15, 'atr_mult': 2.0}` | +6.8 % | 10 |
| 2024-11-17 | 2025-02-16 | `{'n_entry': 30, 'n_exit': 10, 'atr_mult': 3.0}` | -0.6 % | 7 |
| 2025-02-16 | 2025-05-18 | `{'n_entry': 20, 'n_exit': 15, 'atr_mult': 3.0}` | +0.4 % | 10 |
| 2025-05-18 | 2025-08-17 | `{'n_entry': 40, 'n_exit': 15, 'atr_mult': 3.0}` | -0.9 % | 7 |
| 2025-08-18 | 2025-11-17 | `{'n_entry': 40, 'n_exit': 15, 'atr_mult': 3.0}` | +1.5 % | 4 |
| 2025-11-17 | 2026-02-16 | `{'n_entry': 40, 'n_exit': 20, 'atr_mult': 3.0}` | -0.9 % | 3 |
| 2026-02-16 | 2026-05-18 | `{'n_entry': 40, 'n_exit': 30, 'atr_mult': 3.0}` | -0.1 % | 5 |
| 2026-05-18 | 2026-08-17 | `{'n_entry': 40, 'n_exit': 30, 'atr_mult': 3.0}` | -0.3 % | 3 |
| 2026-08-17 | 2026-11-16 | `{'n_entry': 55, 'n_exit': 30, 'atr_mult': 3.0}` | +2.0 % | 4 |
