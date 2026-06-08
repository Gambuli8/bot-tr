# Deployment multi-asset en el VPS (BTC + SOL + AVAX, Futures USDT-M 7×)

> **Actualizado 2026-06-08 (Fase 3)**. El portafolio cambió de 4 a 3 monedas
> (LINK descartado por WFA) y migra de Spot 1× a Futures USDT-M 7×.

## Portafolio aprobado para producción

| Activo | OS retorno 180d (7×) | OS positivas | PF mediano | t/día |
|---|---|---|---|---|
| BTC/USDT | +18.73% | 5/7 | 1.18 | 0.13 |
| SOL/USDT | +11.95% | 4/7 | 1.68 | 0.12 |
| AVAX/USDT | +14.15% | 5/7 | 1.56 | 0.14 |
| **Total** | **+44.83%** | — | — | **~0.39/día** |

Detalle del WFA en `docs/BACKTESTS.md` (Fase 3, 2026-06-08).

## Configuración por bot

```
Mercado:                Binance Futures USDT-M (perpetuos)
Leverage:               7×
Margin mode:            isolated (sin contagio entre trades)
Position mode:          one-way (sin hedge)
Capital nominal/bot:    $70 USDT ($210 / 3)
Risk per trade:         5% del capital (no 8% — margen por riesgo de liquidación)
MAX_CONCURRENT_TRADES:  1 por bot
Engine:                 PriceActionEngine en TF 1h
```

## Risk math (clave para entender el sizing)

| Concepto | Valor |
|---|---|
| Risk por trade | 5% × $70 = $3.50 USDT |
| Notional por posición | $3.50 / SL% (cap por leverage = $70 × 7 = $490) |
| Peor caso 3 trades simultáneos abiertos | 3 × $3.50 = **$10.50 = 5% del capital total** |
| Caída del precio que liquida | ~13% (con 7× e isolated margin) |
| Margen bloqueado por trade (peor caso) | $70 × 5% = $3.50 |

## Por qué 3 containers separados

El bot usa `settings.symbol` (singular). 1 container = 1 símbolo. Refactor a
multi-symbol en una sola instancia no aporta valor neto vs la simplicidad de
3 containers paralelos.

Cada bot:
- Su propio `.env.<sym>`
- Su propio `data/<sym>/` (state, journal, audit, log)
- Su propio container Docker
- Mismas API keys de Binance (Futures Trading habilitado)
- Mismo chat de Telegram (vista unificada)

## Setup paso a paso en el VPS

### 1) Pull del código (con el refactor a Futures)

```bash
ssh ubuntu@<TU_IP>
cd ~/agent-trading

# Bajar los bots viejos (Spot) si estaban corriendo
docker compose -f docker-compose.multi.yml down

# Backup de los envs viejos por si querés rollback
mkdir -p .env-backup-spot
cp .env .env.btc .env.sol .env.avax .env.link .env-backup-spot/ 2>/dev/null

# Pull
git pull origin main
git log --oneline -3
```

### 2) Generar API keys de Binance Futures

⚠ **Las API keys de Spot NO sirven para Futures**. Hay que crear nuevas:

**Testnet Futures (para los 7 días de validación):**
1. Ir a https://testnet.binancefuture.com
2. Registrarse / login (es separado del Spot testnet)
3. Te dan automáticamente $10,000 USDT virtuales en el wallet Futures
4. Settings → API Management → "Create API"
5. **Solo habilitá**: "Enable Futures Trading"
6. **NO habilites**: "Withdraw", "Spot & Margin Trading"
7. Copiá la API key + Secret

**Mainnet Futures (cuando pases a producción real, después de los 7 días):**
1. Binance.com → API Management
2. Crear nueva API key
3. Habilitá: "Enable Futures Trading"
4. NO habilites: "Withdraw", "Spot & Margin Trading"
5. (Recomendado) Restringí por IP a la IP de tu VPS
6. Activá Futures en tu cuenta si todavía no lo hiciste (Futures Wallet → habilitar)

### 3) Crear los 3 `.env` (BTC + SOL + AVAX)

Si tenés `.env.btc` viejo de Spot, hay que actualizarlo. Más rápido reempezar
de `.env.example`:

```bash
cd ~/agent-trading

# Punto de partida: .env.example (que ya tiene la config de Futures)
cp .env.example .env.btc

# Editar — poner las keys reales de Futures testnet
nano .env.btc
```

**Variables clave que tenés que poner/verificar en `.env.btc`:**

```bash
BINANCE_API_KEY=tu_futures_testnet_api_key
BINANCE_API_SECRET=tu_futures_testnet_api_secret
BINANCE_TESTNET=true                  # ⚠ TRUE para los 7 días de validación
TELEGRAM_BOT_TOKEN=tu_token
TELEGRAM_CHAT_ID=tu_chat_id

# TRADING — esto cambia por bot
TRADING_SYMBOL=BTC/USDT
TRADING_TIMEFRAME=1h
ENGINE=price_action
INITIAL_CAPITAL=70
MAX_RISK_PER_TRADE=0.05               # 5% (no 8%)
MAX_CONCURRENT_TRADES=1
TESTING_MODE=true
TESTING_LOOP_SECONDS=120

# FUTURES
LEVERAGE=7
MARGIN_MODE=isolated
```

Guardá. Generá los otros 2:

```bash
cp .env.btc .env.sol  && sed -i 's|TRADING_SYMBOL=BTC/USDT|TRADING_SYMBOL=SOL/USDT|'  .env.sol
cp .env.btc .env.avax && sed -i 's|TRADING_SYMBOL=BTC/USDT|TRADING_SYMBOL=AVAX/USDT|' .env.avax

chmod 600 .env.btc .env.sol .env.avax
```

**Eliminá el `.env.link`** (ya no se usa):

```bash
rm -f .env.link
rm -rf data/link  # si querés borrar el historial — opcional
```

### 4) Crear los `data/<sym>/`

```bash
mkdir -p data/btc data/sol data/avax
ls -la data/
```

### 5) Levantar los 3 bots

```bash
# Build de la imagen actualizada (con el refactor a Futures)
docker compose -f docker-compose.multi.yml build

# Up
docker compose -f docker-compose.multi.yml up -d

# Verificar
docker compose -f docker-compose.multi.yml ps
```

Tras 30-60 segundos los 3 bots deben estar `Up (healthy)`. Si no, ver logs:

```bash
docker compose -f docker-compose.multi.yml logs --tail=80 bot-btc
docker compose -f docker-compose.multi.yml logs --tail=80 bot-sol
docker compose -f docker-compose.multi.yml logs --tail=80 bot-avax
```

### 6) Mirar Telegram

Esperá **3 mensajes `🤖 ¡Arranqué!`** (uno por bot) con su símbolo correcto. Si
no llegan los 3, los logs van a decir el error.

### 7) Verificar conexión a Futures

```bash
for sym in btc sol avax; do
  echo "=== $sym ==="
  docker compose -f docker-compose.multi.yml logs --tail=80 bot-$sym | \
    grep -E "FUTURES|Leverage|Margin mode|Par.*disponible|Precio actual"
done
```

Deberías ver por cada bot:
```
🔧 Modo TESTNET FUTURES activado — testnet.binancefuture.com
✅ Margin mode = ISOLATED para X/USDT
✅ Leverage = 7× para X/USDT
✅ Par X/USDT disponible (Futures perpetuo)
✅ Precio actual X/USDT: $XX (mainnet)
```

## Operación diaria

```bash
# Status rápido
docker compose -f docker-compose.multi.yml ps
for sym in btc sol avax; do
  rc=$(docker inspect --format '{{.RestartCount}}' agent-trading-$sym)
  trades=$(wc -l < data/$sym/trade_journal.jsonl 2>/dev/null || echo 0)
  echo "$sym: Restarts=$rc | Trades=$trades"
done

# Logs vivos
docker compose -f docker-compose.multi.yml logs -f bot-btc

# Stats
docker stats agent-trading-btc agent-trading-sol agent-trading-avax --no-stream

# Stop / start un bot
docker compose -f docker-compose.multi.yml stop bot-avax
docker compose -f docker-compose.multi.yml start bot-avax

# Update tras git pull
git pull
docker compose -f docker-compose.multi.yml build
docker compose -f docker-compose.multi.yml up -d --force-recreate
```

## Validación pre-producción (7 días en Futures testnet)

⚠ **NO migres a `BINANCE_TESTNET=false` hasta cumplir TODO esto:**

| # | Criterio | Verificación |
|---|---|---|
| 1 | 7+ días corridos sin restarts | `RestartCount = 0` en los 3 bots |
| 2 | ≥1 trade cerrado por cada bot | `wc -l data/<sym>/trade_journal.jsonl` ≥ 1 |
| 3 | Leverage y margin se aplicaron OK en cada trade | Buscar `Leverage = 7×` y `Margin mode = ISOLATED` en logs |
| 4 | SL/TP llegaron como `STOP_MARKET` y `LIMIT` reduceOnly | Buscar `🛑 SL colocado en Futures` y `🎯 TP colocado en Futures` en logs |
| 5 | Sin warnings de reconciliación | `grep -i warning data/<sym>/audit.jsonl` vacío |
| 6 | Trades cerraron con PnL en línea con el SL/TP esperados | Inspeccionar últimos 3 trades del journal |

Si todo OK → migrar:

```bash
docker compose -f docker-compose.multi.yml down

# Cambiar las 3 keys a las de mainnet Futures
for env in .env.btc .env.sol .env.avax; do
  nano $env   # cambiar BINANCE_API_KEY y BINANCE_API_SECRET por las de mainnet
  sed -i 's/^BINANCE_TESTNET=true$/BINANCE_TESTNET=false/' $env
done

# Verificar
grep BINANCE_TESTNET .env.btc .env.sol .env.avax

# Up
docker compose -f docker-compose.multi.yml up -d
```

## Inyectar los $200 mensuales

```bash
docker compose -f docker-compose.multi.yml down

# $200 / 3 bots = ~$66.66 más por bot → de 70 a 136.66 el primer mes
for env in .env.btc .env.sol .env.avax; do
  sed -i 's/^INITIAL_CAPITAL=70$/INITIAL_CAPITAL=136.66/' $env
done

docker compose -f docker-compose.multi.yml up -d
```

Los siguientes meses ajustá el valor de partida (`136.66` → `203.33` → ...).

## Recursos del VPS

| Componente | RAM |
|---|---|
| 3× bot containers | ~1.1-1.5 GB |
| autoheal sidecar | ~30 MB |
| Ubuntu + Docker daemon | ~500 MB |
| **Total** | **~2 GB** |

Tu VPS tiene 7.8 GB → sobrado.

## Rollback rápido (volver a Spot 1×)

Si algo sale catastrófico en Futures:

```bash
cd ~/agent-trading
docker compose -f docker-compose.multi.yml down

# Restaurar los envs viejos de Spot
cp .env-backup-spot/.env.btc  .env.btc
cp .env-backup-spot/.env.sol  .env.sol
cp .env-backup-spot/.env.avax .env.avax
cp .env-backup-spot/.env.link .env.link

# Para volver a Spot necesitás también revertir core/exchange.py
git checkout main -- core/exchange.py   # NO, mejor: git checkout al commit pre-Fase3
git log --oneline -10                    # buscar el último commit pre-Fase3
git checkout <hash_pre_fase3> -- core/exchange.py config/settings.py docker-compose.multi.yml

docker compose -f docker-compose.multi.yml build
docker compose -f docker-compose.multi.yml up -d
```

## Diferencias clave Spot 1× → Futures 7×

| Concepto | Spot 1× (Fase 2) | Futures 7× (Fase 3) |
|---|---|---|
| Mercado ccxt | `defaultType: spot` | `defaultType: future` |
| Testnet URL | testnet.binance.vision | **testnet.binancefuture.com** |
| API keys | Spot | **Futures (distintas)** |
| Sizing | 1× notional | 7× notional |
| SL exchange | `STOP_LOSS` (Spot) | `STOP_MARKET` + reduceOnly |
| TP exchange | `LIMIT` (Spot) | `LIMIT` + reduceOnly |
| Liquidación | No existe | ~13% adverso liquida |
| Funding rate | No existe | ±0.01% cada 8h típico |
| Shorts reales | No (solo long) | Sí (long y short) |
| Risk/trade | 8% | 5% (margen vs liquidación) |
| Capital | $52.5 × 4 = $210 | $70 × 3 = $210 |

Para entender el motor PA en sí mismo y las auditorías que lo aprobaron, ver
`docs/BACKTESTS.md` y `docs/HANDOFF.md`.
