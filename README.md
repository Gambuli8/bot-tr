# Bot de trading BingX

Bot para Futuros Perpetuos USDT-M de BingX. **Analiza solo** la estrategia Zona 1D + Fibonacci 1H +
Diagonal 5m con velas de BingX (gratis, sin TradingView pago), opera con **margen fijo de 1–2 USDT**
con SL y TP, y te cuenta todo por Telegram.

```
bingx-bot (Docker, VPS)
  ├─ scanner: cada 5 min baja velas 1D/1H/5m de los 6 pares → motor de estrategia (bot/strategy.py)
  │     eventos: zona → cambio 1H → 0,618 → entrada (o cancelado)
  ├─ ejecutor: valida (duplicados, pausa, slippage, límite diario) → sizing (margen fijo, apalancamiento
  │     mínimo, liquidación vs SL, R:R neto) → BingX: orden + SL + TP (isolated, one-way, reduceOnly)
  ├─ monitor: detecta cierres TP/SL, PnL real, SL faltante, posiciones desconocidas
  ├─ Telegram: narra cada etapa, entradas, salidas · /estado /hoy /semana /mes /pausa /cerrar
  └─ resúmenes semanales y mensuales → Telegram + Google Drive

TradingView (opcional): el indicador tradingview/bingx_fibo_mtf.pine dibuja lo mismo en el gráfico
(funciona en el plan gratis). Con STRATEGY_SOURCE=tradingview las entradas llegan por webhook (plan pago).
```

## Estructura

| Archivo | Qué hace |
|---|---|
| `bot/server.py` | Arranque, `/tv/health`, webhook opcional `/tv/webhook` |
| `bot/strategy.py` | Motor de la estrategia (misma lógica que el Pine) |
| `bot/scanner.py` | Baja velas cada 5 min y le pasa los eventos al ejecutor |
| `bot/signals.py` | Formato del JSON que manda TradingView |
| `bot/executor.py` | Controles y apertura de operaciones |
| `bot/sizing.py` | Margen fijo → apalancamiento, cantidad, riesgo, R:R |
| `bot/bingx.py` | Cliente REST de BingX (firma HMAC-SHA256, demo/real) |
| `bot/monitor.py` | Cierres, PnL, protección, resúmenes programados |
| `bot/narrator.py` | Mensajes en castellano simple |
| `bot/telegram.py` | Envío y comandos |
| `bot/reports.py`, `bot/drive.py` | Resúmenes y subida a Drive |
| `bot/cli.py` | `check` (verifica todo), `replay` (motor sobre velas reales) y `test-signal` |
| `tradingview/bingx_fibo_mtf.pine` | La estrategia para ver en el gráfico (y alertas opcionales) |
| `docs/STRATEGY.md` | Reglas exactas de la estrategia |
| `docs/DEPLOY.md` | Puesta en marcha: BingX, Telegram, VPS, TradingView, Drive, demo → real |

## Desarrollo local

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements-dev.txt
pytest -q
```

El bot anterior (Binance) está archivado en la rama `legacy/binance-bot` y el tag `legacy-binance-v1`.
