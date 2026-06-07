# Despliegue en VPS Ubuntu (Docker Compose)

Cinco pasos para poner el bot 24/7 en un VPS Ubuntu (probado en 22.04 y 24.04).

## Lo que vas a necesitar antes

- Un VPS con Ubuntu (1 vCPU, 1 GB RAM, 10 GB disco alcanzan)
- Acceso SSH (`ssh user@ip`) y sudo
- Tus credenciales de Binance Futures USDT-M (API_KEY + SECRET)
- Token y chat_id de Telegram (BotFather + @userinfobot)

---

## Paso 1 — Conectarse al VPS e instalar Docker

```bash
ssh ubuntu@<IP_DEL_VPS>

# Actualizar el sistema
sudo apt-get update && sudo apt-get -y upgrade

# Instalar Docker y Docker Compose (oficial de Docker)
curl -fsSL https://get.docker.com | sudo sh
sudo apt-get install -y docker-compose-plugin

# Permitir usar docker sin sudo (logout/login después de esto)
sudo usermod -aG docker $USER
newgrp docker

# Verificar
docker --version && docker compose version
```

## Paso 2 — Clonar el repo

```bash
cd ~
git clone https://github.com/Gambuli8/bot-tr.git agent-trading
cd agent-trading
```

## Paso 3 — Configurar el `.env` con tus claves

```bash
cp .env.example .env
nano .env
```

Ajustá al menos estas variables:

```bash
# Binance Futures USDT-M (NO uses keys de Spot)
BINANCE_API_KEY=tu_api_key_real
BINANCE_API_SECRET=tu_api_secret_real
BINANCE_TESTNET=false               # true si querés probar primero sin plata real

# Telegram (BotFather + @userinfobot)
TELEGRAM_BOT_TOKEN=123456:abc-def
TELEGRAM_CHAT_ID=987654321

# Capital y riesgo (los valores del playbook)
INITIAL_CAPITAL=210
MAX_RISK_PER_TRADE=0.08             # 8% del capital
DAILY_DRAWDOWN_LIMIT=0.05

# Modo 24/7 (vacío = sin schedule)
ACTIVE_HOURS_UTC=
MAX_CONCURRENT_TRADES=2

# Motor: "scalping" (5m BB squeeze) recomendado tras los backtests
ENGINE=scalping
TRADING_TIMEFRAME=5m
TESTING_LOOP_SECONDS=60
TESTING_MODE=true                   # true = sin Claude API (usa engine local)
```

Guardá con `Ctrl+O`, `Enter`, `Ctrl+X`.

## Paso 4 — Levantar el bot

```bash
# Build de la imagen (la primera vez tarda ~2-3 min)
docker compose build

# Arrancar en background
docker compose up -d

# Ver que esté vivo
docker compose ps
docker compose logs -f --tail=50
```

Deberías ver el mensaje `🚀 Bot arrancado` en los logs, y una notificación a tu Telegram con `🤖 ¡Arranqué!`.

## Paso 5 — Monitoreo y operación diaria

```bash
# Ver logs en vivo
docker compose logs -f bot

# Parar el bot
docker compose down

# Re-arrancar (mantiene state.json)
docker compose up -d

# Actualizar el código (después de un git pull)
git pull
docker compose build
docker compose up -d --force-recreate

# Ver uso de recursos
docker stats agent-trading
```

**Desde Telegram** vas a poder hacer:

- `/status` — capital, win rate, drawdown
- `/position` — operaciones abiertas en vivo
- `/pnl` — P&L 24h / 7d / total
- `/off` y `/on` — pausar / reactivar aperturas
- `/close` → `/close_confirm` — cerrar a mercado
- `/stop` → `/stop_confirm` — apagar el proceso (el container se reinicia solo por `restart: unless-stopped`)

---

## Troubleshooting rápido

**El bot no manda Telegram al arrancar:**
- Verificá `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en el `.env`
- Mandale `/start` al bot **desde tu cuenta** una vez para autorizarlo

**El container se reinicia en loop:**
```bash
docker compose logs --tail=200 bot
```
Mirá el último error. Si es de Binance (auth), revisá las API keys.

**Quiero ver el state actual:**
```bash
cat data/state.json | python3 -m json.tool
cat data/trade_journal.jsonl | tail -5
```

**Cambié una variable del `.env`:**
```bash
docker compose down && docker compose up -d
```
(no hace falta rebuild para cambios del `.env`).

**Quiero proteger las API keys con permisos:**
```bash
chmod 600 .env
```

## Seguridad

- **API keys de Binance**: dale permisos sólo de "Enable Futures" + "Enable Trading". **NO** "Withdraw".
- **Firewall**: `sudo ufw enable && sudo ufw allow ssh` (el bot no expone puertos, no necesita más).
- **fail2ban**: `sudo apt install fail2ban` para evitar brute force al SSH.
- **Backups del state**: cron diario que copie `data/` a un bucket S3/Backblaze por las dudas.

## Para producción seria

- Considerá usar [Watchtower](https://containrrr.dev/watchtower/) para auto-update de la imagen.
- Logs centralizados con Loki/Promtail si tenés más de un bot.
- Alertas dual-channel: Telegram + email (vía `notify_critical` también podría tirar a un webhook).

---

## Monitoreo 24/7: heartbeat + dead-man's switch (roadmap #1)

El bot tiene **dos capas** para que NO se quede colgado en silencio:

### 1. Heartbeat local + healthcheck de Docker (incluido, sin config)

`health.beat()` escribe `data/heartbeat` (epoch) al final de **cada ciclo** del
loop. El `HEALTHCHECK` del `Dockerfile` lo lee: si quedó viejo (>5 min, el loop
se colgó), Docker marca el container **`unhealthy`**.

```bash
docker compose ps            # mirá la columna STATUS: "healthy" / "unhealthy"
cat data/heartbeat           # epoch del último ciclo
docker inspect --format '{{.State.Health.Status}}' agent-trading
```

El sidecar **`autoheal`** (en el `docker-compose.yml`) vigila ese estado y
**reinicia el bot automáticamente** si queda `unhealthy` — porque
`restart: unless-stopped` NO reinicia por unhealthy, sólo por exit.

> Seguridad: `autoheal` monta el `docker.sock` (read-only). Es lo estándar para
> auto-restart, pero si preferís no exponerlo, sacá ese service: el dead-man's
> switch externo igual te avisa y reiniciás a mano con `docker compose restart bot`.

### 2. Dead-man's switch externo (recomendado — detecta muerte del VPS)

El healthcheck interno no sirve si **se cae el VPS entero o Docker**. Para eso,
un servicio externo que te avise cuando el bot deja de dar señales:

1. Creá una check gratis en [healthchecks.io](https://healthchecks.io) (período
   ej. 5 min, grace 5 min). Te da una URL de ping.
2. Pegala en el `.env`:
   ```bash
   HEALTHCHECK_URL=https://hc-ping.com/tu-uuid-aca
   ```
3. `docker compose up -d`. El bot pinguea esa URL en cada ciclo (`/start` al
   arrancar, ping en cada ciclo OK, `/fail` si crashea).

Si los pings dejan de llegar (VPS apagado, red caída, Docker muerto, loop
colgado), healthchecks.io te manda alerta a Telegram/email. Es la única forma de
enterarte **desde afuera** de que el bot murió.
