# Puesta en marcha

Orden recomendado: BingX → Telegram → VPS → prueba de alertas → TradingView → 5 días demo → real.

## 1. BingX: API key

1. BingX → Perfil → **Gestión de API** → Crear API.
2. Permisos: **Perpetual Futures / Trade**. **NO** habilitar retiros.
3. Restricción por IP: `13.140.143.209` (el VPS).
4. Guardá API Key y Secret (el secret se muestra una sola vez).
5. En la app, activá **Demo Trading** (saldo VST) para la etapa de prueba.

> Las claves las cargás vos en el `.env` del VPS. No las pegues en chats ni en TradingView.

## 2. Telegram

Se puede reutilizar el bot de Telegram anterior (el viejo ya está apagado). Hacen falta
`TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (tu chat con el bot).

## 3. VPS

```bash
ssh root@13.140.143.209
cd /root/agent-trading
git fetch && git checkout main && git pull
mkdir -p data/bingx
cp .env.example .env
nano .env        # BINGX_API_KEY, BINGX_API_SECRET, BINGX_MODE=demo, WEBHOOK_SECRET, TELEGRAM_*
docker compose up -d --build
docker compose exec bingx-bot python -m bot.cli check
```

`check` tiene que mostrar: firma OK, saldo VST, modo one-way, y el apalancamiento que usaría en cada par.

### Exponer el webhook por HTTPS (Caddy existente)

En `/root/n8n-financiero/Caddyfile`:

```
ggambuli-n8n.online {
    encode zstd gzip
    handle /tv/* {
        reverse_proxy bingx-bot:8080
    }
    handle {
        reverse_proxy n8n:5678
    }
}
```

```bash
docker exec n8n-caddy caddy reload --config /etc/caddy/Caddyfile
curl https://ggambuli-n8n.online/tv/health
```

## 4. Probar los mensajes antes de conectar TradingView

Desde el VPS (simula alertas; en demo también se puede probar una entrada real con VST):

```bash
docker compose exec bingx-bot python -m bot.cli test-signal --event zone  --symbol BTC-USDT --side LONG
docker compose exec bingx-bot python -m bot.cli test-signal --event choch --symbol BTC-USDT --side LONG
docker compose exec bingx-bot python -m bot.cli test-signal --event fib   --symbol BTC-USDT --side LONG
docker compose exec bingx-bot python -m bot.cli test-signal --event entry --symbol BTC-USDT --side LONG   # sólo demo
```

Después de la entrada de prueba: `/estado` en Telegram, verla en BingX Demo con su SL y TP, y cerrarla con
`/cerrar BTC si` para comprobar el aviso de cierre con el PnL.

## 5. TradingView

Requisitos: **plan pago** (los webhooks no están en el plan gratis) y **2FA activado**.

1. Pine Editor → pegar `tradingview/bingx_fibo_mtf.pine` → Guardar → Agregar al gráfico.
2. Abrir el gráfico **5 minutos** de `BINGX:BTCUSDT.P`.
3. Configuración del indicador → `WEBHOOK_SECRET` = el mismo del `.env`.
4. Crear alerta:
   - Condición: **BingX Fibo MTF** → **Cualquier llamada a la función alert()**
   - Vencimiento: sin vencimiento
   - Notificaciones → **URL de webhook**: `https://ggambuli-n8n.online/tv/webhook`
5. Repetir 2–4 para `ETHUSDT.P`, `SOLUSDT.P`, `XRPUSDT.P`, `ZECUSDT.P`, `DOGEUSDT.P`.

Si cambiás un parámetro del indicador, hay que **borrar y recrear** la alerta (TradingView congela la configuración al crearla).

## 6. Google Drive (resúmenes)

1. [Google Cloud Console](https://console.cloud.google.com/) → proyecto nuevo → habilitar **Google Drive API**.
2. Pantalla de consentimiento OAuth → Externo → publicar la app (si queda "en prueba", el token vence a los 7 días).
3. Credenciales → ID de cliente OAuth → **App de escritorio**.
4. En tu PC: `python -m bot.google_auth --client-id ... --client-secret ...` → autorizás en el navegador.
5. Copiá `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` y `GOOGLE_REFRESH_TOKEN` al `.env` del VPS y
   `docker compose up -d`.

Los resúmenes quedan en **Mi unidad / Bot Trading BingX / Semanales** y **/ Mensuales**, como Google Docs.

## 7. Cinco días en demo → checklist para pasar a real

- [ ] Todas las alertas del registro de TradingView aparecen en `data/events.jsonl` (ninguna perdida).
- [ ] Al menos una operación completa: entrada → cierre por TP o SL, con aviso y PnL igual al de BingX.
- [ ] Cero posiciones sin SL, cero órdenes duplicadas, cero errores sin explicar en `data/bot.log`.
- [ ] `/pausa`, `/reanudar`, `/cerrar` probados.
- [ ] Resumen semanal recibido en Telegram y en Drive.
- [ ] Revisaste cada entrada en el gráfico y coincide con lo que harías a mano.

Para pasar a real: en `.env` poner `BINGX_MODE=live` (y el margen que quieras, 1–2 USDT) →
`docker compose up -d` → `python -m bot.cli check` muestra el saldo real.

## 8. Modo carry (captura de funding)

Compra spot + short del mismo tamaño en 4 pares (BTC, ETH, DOGE, XRP) para cobrar el funding sin
exposición al precio. Detalle de la lógica en `bot/carry.py`; validación en `scripts/backtest_funding.py`.

1. **Permisos de la API key** (BingX → Gestión de API → editar): además de Perpetual Futures, habilitar
   **Spot Trading** y **transferencias internas / Universal Transfer**. **Nunca** habilitar retiros.
2. En el `.env`:
   ```
   CARRY_ENABLED=true
   CARRY_SYMBOLS=BTC-USDT,ETH-USDT,DOGE-USDT,XRP-USDT
   CARRY_CAPITAL_USDT=200
   CARRY_LEVERAGE=2
   ```
3. `docker compose up -d` y en Telegram `/carry`.

Qué hace solo: arma cada par (si hay una operación direccional abierta en ese par, espera a que cierre),
protege el short si el precio sube ±15 % (primero con el funding acumulado, después vendiendo spot),
reinvierte lo ganado cuando supera el 3 % del capital del par y alcanza para al menos un paso de contrato,
y corrige la cobertura si spot y short se desbalancean. Para desarmar: `/carry cerrar BTC si` o
`/carry cerrar todo si`. Los pares del carry quedan fuera de la estrategia direccional.
