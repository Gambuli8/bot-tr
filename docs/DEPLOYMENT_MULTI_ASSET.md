# Deployment multi-asset en el VPS (BTC + SOL + AVAX + LINK)

Tras la auditoría Fase 2 (`docs/BACKTESTS.md`, sección 2026-06-07 Fase 2),
el portafolio aprobado para producción es:

| Activo | Edge | OS retorno 180d | t/día |
|---|---|---|---|
| BTC/USDT | ✅ núcleo | +25.05% | 0.13 |
| SOL/USDT | ✅ núcleo | +38.46% | 0.12 |
| AVAX/USDT | 🟡 borde | +8.46% | 0.16 |
| LINK/USDT | 🟡 borde | +6.13% | 0.11 |
| **Total** | | **+78.10%** | **~0.52** |

## Por qué 4 containers separados

El bot actual usa `settings.symbol` (singular) en todo el ciclo de la
estrategia. Soportar 4 símbolos en una sola instancia requeriría refactor
profundo de `strategies/main_strategy.py`, `core/exchange.py` y del
`OrderManager` para manejar pools de posición segmentados por símbolo.

La alternativa limpia y rápida: **4 instancias del mismo container, una
por símbolo, compartiendo el repo y la API key**, cada una con su `.env`
específico. Telegram va a recibir señales de los 4 en el mismo chat
(ventaja: vista unificada).

## Distribución del capital

Capital nominal del playbook: **$210 USDT**. Dividir en 4 buckets:

| Bot | Capital nominal | Risk/trade | Risk USDT máx |
|---|---|---|---|
| BTC | $52.5 | 8% | $4.20 |
| SOL | $52.5 | 8% | $4.20 |
| AVAX | $52.5 | 8% | $4.20 |
| LINK | $52.5 | 8% | $4.20 |
| **Total exposición simultánea peor caso** | $210 | — | **$16.80 (8% global)** |

**Por qué dividir en 4** en lugar de poner $210 en cada bot: si los 4 bots
abren posición al mismo tiempo (mercado tendencial), sumás 4× la exposición
que pensabas. Dividiendo a priori se respeta el `MAX_RISK_PER_TRADE=8%`
del playbook a **nivel portafolio**, no a nivel bot individual.

Las inyecciones mensuales de $200 USDT se distribuyen igual: +$50 a cada
bot el primer día del mes.

## MAX_CONCURRENT_TRADES

**Cada bot operará con `MAX_CONCURRENT_TRADES=1`**. Justificación:

- El PriceActionEngine en 1h dispara ~1 setup cada 5-10 días por símbolo
  según el WFA. No tiene sentido sobre-trade dentro del mismo símbolo.
- Permitir hasta 4 trades concurrentes (uno por bot) ya da la diversificación
  buscada.
- Si querés flexibilidad, podés subir a `MAX_CONCURRENT_TRADES=2` por bot,
  pero ojo: cuadruplica el risk teórico (16% por bot × 4 bots = 32%).

## Estructura de archivos en el VPS

```
~/agent-trading/
├── .env.btc            # TRADING_SYMBOL=BTC/USDT, ENGINE=price_action
├── .env.sol            # TRADING_SYMBOL=SOL/USDT, ENGINE=price_action
├── .env.avax           # TRADING_SYMBOL=AVAX/USDT, ENGINE=price_action
├── .env.link           # TRADING_SYMBOL=LINK/USDT, ENGINE=price_action
├── docker-compose.multi.yml  # 4 servicios + autoheal compartido
├── data/
│   ├── btc/            # state.json, trade_journal.jsonl del bot BTC
│   ├── sol/
│   ├── avax/
│   └── link/
└── ...resto del repo...
```

Cada bot mantiene su `data/<sym>/` separado para que los `state.json`
y `trade_journal.jsonl` no se pisen.

## Setup paso a paso en el VPS

### 1) Pull del código

```bash
cd ~/agent-trading
git pull
```

### 2) Crear los 4 .env

Base común que todos comparten (las claves Binance + Telegram son las
mismas; el bot diferencia trades por `client_order_id` con prefijo del
símbolo).

**.env.btc** (similar para los otros, solo cambia `TRADING_SYMBOL` y
opcionalmente `INITIAL_CAPITAL`):

```bash
# Binance (las mismas keys para todos los bots — Binance soporta multi-conexión por API key)
BINANCE_API_KEY=tu_api_key_real
BINANCE_API_SECRET=tu_api_secret_real
BINANCE_TESTNET=true   # ⚠ false SOLO cuando hayan corrido 7+ días limpios en testnet

# Telegram (el mismo chat para todos — vas a ver señales unificadas)
TELEGRAM_BOT_TOKEN=tu_token
TELEGRAM_CHAT_ID=tu_chat_id

# Símbolo y motor — esto cambia por bot
TRADING_SYMBOL=BTC/USDT
TRADING_TIMEFRAME=1h
ENGINE=price_action

# Capital y riesgo
INITIAL_CAPITAL=52.5
MAX_RISK_PER_TRADE=0.08
DAILY_DRAWDOWN_LIMIT=0.05
MAX_CONCURRENT_TRADES=1     # un solo trade simultáneo POR BOT

# Misc
TESTING_MODE=true
TESTING_LOOP_SECONDS=120     # PA en 1h no necesita poll cada minuto — 2 min basta
ACTIVE_HOURS_UTC=
HEALTHCHECK_URL=             # opcional: dead-man's switch

# Filtro de scalping descartado (PA no lo usa pero lo dejamos vacío por higiene)
SCALP_SKIP_HOURS_UTC=
```

Para crear los 4 .env de una sola vez:

```bash
cd ~/agent-trading

# Copiar el de BTC como punto de partida y editarlo a mano (o con sed)
cp .env.example .env.btc
nano .env.btc   # poner valores reales

# Clonar y modificar para los otros 3
cp .env.btc .env.sol  && sed -i 's|TRADING_SYMBOL=BTC/USDT|TRADING_SYMBOL=SOL/USDT|' .env.sol
cp .env.btc .env.avax && sed -i 's|TRADING_SYMBOL=BTC/USDT|TRADING_SYMBOL=AVAX/USDT|' .env.avax
cp .env.btc .env.link && sed -i 's|TRADING_SYMBOL=BTC/USDT|TRADING_SYMBOL=LINK/USDT|' .env.link

chmod 600 .env.btc .env.sol .env.avax .env.link
```

### 3) Levantar los 4 bots con `docker-compose.multi.yml`

El repo ahora trae `docker-compose.multi.yml` listo. Levantalos:

```bash
cd ~/agent-trading

# Build una sola vez (la imagen es compartida por los 4 bots)
docker compose -f docker-compose.multi.yml build

# Up de los 4 en background
docker compose -f docker-compose.multi.yml up -d

# Ver que estén vivos
docker compose -f docker-compose.multi.yml ps

# Logs de uno específico
docker compose -f docker-compose.multi.yml logs -f bot-btc

# Logs de los 4 mezclados
docker compose -f docker-compose.multi.yml logs -f
```

Deberías ver 4 notificaciones a Telegram con `🤖 ¡Arranqué!` y el símbolo
correspondiente.

### 4) Operación diaria

```bash
# Parar uno solo
docker compose -f docker-compose.multi.yml stop bot-avax

# Parar todos
docker compose -f docker-compose.multi.yml down

# Update tras git pull
git pull
docker compose -f docker-compose.multi.yml build
docker compose -f docker-compose.multi.yml up -d --force-recreate

# Stats de recursos
docker stats $(docker ps --filter "name=agent-trading-" --format "{{.Names}}")
```

Desde Telegram **cada bot responde a los comandos por separado** porque
todos comparten el mismo `TELEGRAM_CHAT_ID`. Si querés controlar uno
específico vas a tener que diferenciarlos. Opciones:
- Usar 4 chats de Telegram distintos (más limpio pero molesto)
- Aceptar que `/status` te va a traer 4 respuestas (una por bot)
- Roadmap futuro: agregar prefijo del símbolo a los comandos (`/status:btc`)

### 5) Validación antes de ir a real

⚠ **REGLA DEL PLAYBOOK**: NO migrar a `BINANCE_TESTNET=false` hasta:
- 7+ días corridos en testnet sin crashes ni errores
- Al menos 1 trade cerrado por cada uno de los 4 bots
- Los `trade_journal.jsonl` muestran consistencia con lo esperado
  (TP/SL ejecutándose en precios razonables, fees en línea)
- Reconciliaciones con el exchange sin discrepancias (revisar
  `data/<sym>/audit.jsonl` por warnings)

## Recursos del VPS

Cada container ocupa ~150-200 MB de RAM. Con 4 containers + autoheal + el
SO base:

| Componente | RAM |
|---|---|
| 4× bot containers | ~800 MB |
| autoheal sidecar | ~30 MB |
| Ubuntu + Docker daemon | ~500 MB |
| **Total** | **~1.3 GB** |

Un VPS con 1 GB de RAM **NO alcanza** para los 4 bots. Recomendación
mínima: **2 GB RAM, 2 vCPU**. El Contabo Cloud VPS S (4 GB / 4 vCPU)
está sobrado.

## Rollback rápido

Si algo sale mal y querés volver al bot single-symbol que tenías antes:

```bash
docker compose -f docker-compose.multi.yml down
docker compose up -d   # vuelve al bot original con tu .env clásico
```

El `data/` original sigue intacto porque los multi-bots usan `data/<sym>/`.
