# Bot de trading BingX (TradingView → VPS → BingX)

Bot para Futuros Perpetuos USDT-M de BingX. La estrategia corre en TradingView (Pine Script);
el bot en el VPS recibe las alertas, valida, calcula el tamaño con **margen fijo de 1–2 USDT**,
opera en BingX con SL y TP, y te cuenta todo por Telegram.

```
TradingView (5m, un indicador por par)
  │  webhook JSON: zone → choch → fib → entry  (o cancel)
  ▼
https://ggambuli-n8n.online/tv/webhook  (Caddy, HTTPS)
  ▼
bingx-bot (FastAPI, Docker)
  ├─ valida: secret, IP de TradingView, duplicados, pausa, antigüedad, slippage
  ├─ sizing: margen fijo → apalancamiento mínimo · liquidación vs SL · R:R neto
  ├─ BingX: orden a mercado + SL + TP (isolated, one-way, cierres reduceOnly)
  ├─ monitor: detecta cierres TP/SL, PnL real, SL faltante, posiciones desconocidas
  ├─ Telegram: narra cada etapa, entradas, salidas · comandos /estado /pausa /cerrar…
  └─ resúmenes semanales y mensuales → Telegram + Google Drive
```

## Estructura

| Archivo | Qué hace |
|---|---|
| `bot/server.py` | Webhook `/tv/webhook`, `/tv/health`, arranque |
| `bot/signals.py` | Formato del JSON que manda TradingView |
| `bot/executor.py` | Controles y apertura de operaciones |
| `bot/sizing.py` | Margen fijo → apalancamiento, cantidad, riesgo, R:R |
| `bot/bingx.py` | Cliente REST de BingX (firma HMAC-SHA256, demo/real) |
| `bot/monitor.py` | Cierres, PnL, protección, resúmenes programados |
| `bot/narrator.py` | Mensajes en castellano simple |
| `bot/telegram.py` | Envío y comandos |
| `bot/reports.py`, `bot/drive.py` | Resúmenes y subida a Drive |
| `bot/cli.py` | `check` (verifica todo) y `test-signal` (alertas de prueba) |
| `tradingview/bingx_fibo_mtf.pine` | La estrategia (indicador con alertas) |
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
