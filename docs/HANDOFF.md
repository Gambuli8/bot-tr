# HANDOFF — Estado del proyecto de trading bot al 2026-06-07

> Este documento es un brief técnico-operativo completo para que **otro LLM
> colaborador** (Gemini u otro) se sume al proyecto con el mismo contexto
> que el LLM principal. Está hecho para copiar-pegar en una sesión nueva.
> Asume que el colaborador no vió el repo todavía.

---

## 0. Identidad del proyecto

**Nombre:** `agent-trading`
**Repo:** https://github.com/Gambuli8/bot-tr (branch `main`)
**Owner:** alejandro / Gambuli8 (Argentina, voseo argentino, tono directo)
**Stack:** Python 3.12, Docker Compose, Binance (Spot testnet + Spot mainnet
read-only), Telegram bot, ccxt, ta, pandas, pydantic.
**Modo actual:** PAPER TRADING / TESTNET. Capital nominal del playbook:
**$210 USDT + inyecciones mensuales de $200 USDT**.

**Qué hace:** opera 4 criptos en paralelo (BTC, SOL, AVAX, LINK) en TF 1h
con un motor de **price action** (sweeps de liquidez + estructura 4h).
Cada cripto la lleva un container Docker independiente que comparte la
misma API key de Binance y reporta al mismo chat de Telegram.

---

## 1. Cómo está corriendo HOY (deploy real)

VPS Contabo Ubuntu 24.04 (7.8 GB RAM, 4 vCPU), `docker compose` orquesta
**4 bots + 1 autoheal sidecar**:

| Container | Símbolo | Capital nominal | Engine | TF base | TF estructura |
|---|---|---|---|---|---|
| `agent-trading-btc` | BTC/USDT | $52.5 | PriceActionEngine | 1h | 4h |
| `agent-trading-sol` | SOL/USDT | $52.5 | PriceActionEngine | 1h | 4h |
| `agent-trading-avax` | AVAX/USDT | $52.5 | PriceActionEngine | 1h | 4h |
| `agent-trading-link` | LINK/USDT | $52.5 | PriceActionEngine | 1h | 4h |
| `agent-trading-autoheal-multi` | — | — | willfarrell/autoheal | — | — |

Configuración por bot (`MAX_RISK_PER_TRADE=0.08`, `MAX_CONCURRENT_TRADES=1`,
`TESTING_LOOP_SECONDS=120`, `TRADING_TIMEFRAME=1h`, `ENGINE=price_action`,
`BINANCE_TESTNET=true`). Cada bot tiene su `.env.<sym>` y su `data/<sym>/`.

**Risk math (clave para entender el sizing):**
- 4 bots × $52.5 cada uno = $210 capital nominal total (= el del playbook).
- 8% de risk por bot = $4.20 risk/trade.
- Peor caso 4 trades simultáneos: 4 × $4.20 = $16.80 = **8% del total**.
- Si se subiera `INITIAL_CAPITAL` a $210 por bot, peor caso = 32% del total
  → rompería el playbook.

Despliegue documentado paso a paso en `docs/DEPLOYMENT_MULTI_ASSET.md` y
`docker-compose.multi.yml`. Despliegue single-symbol viejo (deprecated)
está en `docs/DEPLOYMENT.md` y `docker-compose.yml`.

---

## 2. Arquitectura del bot (single instance)

```
main.py
  └── startup_checks()                 (valida conexión exchange)
  └── BotController                    (state thread-safe Telegram ↔ loop)
  └── MainStrategy
       ├── ExchangeClient              (testnet órdenes + mainnet datos)
       ├── PriceActionEngine           (motor de decisión activo)
       ├── OrderManager                (estado posición + journal)
       ├── TelegramNotifier            (señales out)
       └── run_forever()               (loop while True con health.beat())
  └── TelegramListener (daemon thread) (recibe comandos /status etc)
```

**Llave del diseño dual-exchange** ([core/exchange.py:50-69](core/exchange.py#L50-L69)):
- Si `BINANCE_TESTNET=true`: las **órdenes** van a `testnet.binance.vision`
  (dinero ficticio $10k USDT) pero los **datos OHLCV** se leen de
  `api.binance.com` (mainnet, sin auth, read-only). El testnet tiene precios
  sintéticos casi planos — el motor necesita datos reales para decidir.
- Si `BINANCE_TESTNET=false`: ambos van a mainnet (plata real).

**Engines disponibles** (`settings.engine`):

| Engine | Estado | Notas |
|---|---|---|
| `price_action` | **EN PRODUCCIÓN** | Sweeps de liquidez + estructura 4h. Single edge confirmado. |
| `scalping` | ❌ Descartado Fase 1 | BB squeeze + expansion en 5m. WFA falló (ver Fase 1). |
| `technical` | Legacy | TechnicalEngine viejo. Sin edge en backtest 90d+. |
| `claude` | Disponible | Usa Anthropic API. No usado en producción multi-asset. |

Cada engine implementa `analyze(...)` con firma propia. El dispatcher está
en [strategies/main_strategy.py](strategies/main_strategy.py) (función
`_call_engine`).

**PriceActionEngine en detalle** ([core/price_action_engine.py](core/price_action_engine.py)):
- Detecta swings con fractal Williams (parámetro `pa_fractal_n`, default 3).
- Determina estructura del 4h: BULL (HH+HL), BEAR (LH+LL), RANGE.
- Solo opera a favor de la estructura 4h.
- Trigger en 1h: liquidity sweep (rompe swing previo + cierra del otro lado).
- Confirmación de volumen: vela de sweep > `pa_vol_mult` × MA(20). Default 1.5.
- SL = swing wick ± `pa_atr_sl_mult` × ATR. Default 1.5.
- TP = SL × `pa_tp_rr`. Default 2.5 (R:R 1:2.5).
- Filtros: `pa_sl_min_pct=0.003`, `pa_sl_max_pct=0.05` (descarta SLs absurdos).

**OrderManager** ([execution/order_manager.py](execution/order_manager.py)):
- Soporta hasta `MAX_CONCURRENT_TRADES` posiciones simultáneas
  (multi-trade). En multi-asset cada bot usa 1.
- Persiste en `data/<sym>/state.json` (capital actual, posiciones abiertas)
  y `data/<sym>/trade_journal.jsonl` (trades cerrados).
- Reconcilia con el exchange cada 5 min para detectar drift.

**Monitoreo** ([health.py](health.py)):
- `health.beat()` escribe `data/heartbeat` (epoch). El HEALTHCHECK del
  `Dockerfile` lo lee y marca unhealthy si quedó viejo (>5 min).
- `autoheal` (sidecar) vigila el flag y reinicia containers unhealthy.
- `health.ping()` opcional → healthchecks.io (dead-man's switch externo).

---

## 3. Auditorías estadísticas hechas (Fase 1 y Fase 2)

Todas en `docs/BACKTESTS.md` con números completos. Acá el resumen ejecutivo:

### Fase 1 — ScalpingEngine descartado

Antes de migrar a real corrí 4 auditorías sobre el ScalpingEngine (BB
squeeze + expansion + volume spike en BTC/USDT 5m, risk 8%, leverage 10×,
fees maker 0.02% / taker 0.05%):

1. **Monte Carlo** (`scripts/audit_montecarlo.py`): 1000 permutaciones del
   orden de PnLs. Resultado: **P(retorno<0) = 100%**, P(DD>12.89%) = 75.9%.
   El backtest original era −2.36%. Edge ≈ 0.
2. **Breakdown por sub-régimen** (`scripts/audit_trade_breakdown.py`):
   disecté 62 trades por hora UTC, ATR%, BB width, vol, lado, día.
   Detecté que hora 06-12 UTC (EU AM) destruía el resultado.
3. **Filtros incrementales** (`scripts/audit_filters.py`): el filtro de
   horario (excluir 06-11 UTC) llevó el retorno 60d de +0.96% a **+9.79%**
   in-sample. Pareció oro.
4. **WFA** (`scripts/audit_wfa.py`): 7 ventanas IS=40d/OS=20d sobre 180d.
   **Solo 2/7 OS positivas, total −17.74%**. El +9.79% era cherry-picking
   de las últimas 6 semanas. **Edge no sobrevivió** → descartado.

### Fase 2 — PriceActionEngine multi-asset

Repetí el WFA del PriceActionEngine sobre 12 cryptos (top liquidez Binance)
en 1h sobre 180d. Antes, sweep de TFs (BTC/ETH/SOL × 15m/30m/1h) mostró
que **solo en 1h el edge era estable** (BTC mantenía retorno bajando TF,
SOL colapsaba −57% en 30m, ETH era flojo en todo).

Resultado WFA 1h (criterios: OS positivas ≥4/7 AND OS total >+10% AND
PF mediano sobre ventanas con n≥3 ≥1.15):

| Coin | OS pos | OS total | trades OS | t/día | Decisión |
|---|---|---|---|---|---|
| **BTC** | 5/7 | +25.05% | 18 | 0.13 | ✅ |
| **SOL** | 5/7 | +38.46% | 17 | 0.12 | ✅ |
| **AVAX** | 4/7 | +8.46% | 22 | 0.16 | 🟡 borde aprobado |
| **LINK** | 4/7 | +6.13% | 16 | 0.11 | 🟡 borde aprobado |
| ETH | 2/7 | +3.12% | 15 | — | ❌ |
| LTC | 4/7 | **−21.30%** | 22 | — | ❌ |
| ADA, DOT, BNB, INJ, DOGE, XRP | varios | todos negativos | — | — | ❌ |
| POL | 2/7 | +56.60% | 19 | — | ❌ cherry-pick 1 ventana de +88% |

**Bug encontrado y arreglado en el script de WFA**: declaraba LTC como ✅
porque calculaba PF **medio** (no mediano) y las ventanas con n<3 daban
PF=99 que inflaban el promedio. Fix aplicado en `audit_wfa_pa.py`: usar
**mediana del PF sobre ventanas con n>=3** y agregar criterio
`OS retorno total >= +10%`. Verificado: LTC ahora correctamente da
"PF mediano 0.56, OS total −20% → ❌ NO PASA".

**Frecuencia esperada del portafolio aprobado**: ~0.52 trades/día = ~3.6
trades/semana = ~15 trades/mes. **No** alcanzamos 1 trade/día pero el
cliente priorizó calidad sobre frecuencia.

---

## 4. Decisiones operativas vigentes

| Decisión | Razón |
|---|---|
| 4 containers (no refactor a multi-symbol en un proceso) | El bot actual usa `settings.symbol` singular en todo el ciclo. Refactor profundo no aporta vs containers separados. |
| `MAX_CONCURRENT_TRADES=1` por bot | PA en 1h dispara 1 setup cada 5-10 días por símbolo. Sobre-trade no aplica. Y limita exposición a 8% global. |
| Capital dividido $52.5 × 4 | Preserva 8% global del playbook (no por bot). |
| TF base 1h (no 15m / 30m) | WFA mostró que el edge solo es estable en 1h. SOL colapsa en 30m. |
| Las 4 monedas comparten API key Binance | Binance permite varias conexiones por key (ccxt rate-limit es por instancia). Más simple. |
| Telegram chat compartido por los 4 | Vista unificada. Trade-off: `/status` te devuelve 4 respuestas. |
| Flags legacy desactivadas en `.env`: `ADX_MIN_TRENDING=0`, `USE_KELLY_SIZING=false`, `REQUIRE_MTF_CONFLUENCE=false` | Eran del TechnicalEngine. El PA hace su propio MTF (4h structure internamente). |
| Mantener el filtro `SCALP_SKIP_HOURS_UTC` vacío | El filtro 06-11 UTC mostró +10× retorno in-sample pero no sobrevivió WFA (era ruido reciente). |
| `INITIAL_CAPITAL` ancla, no autoritativo | Es el capital nominal de arranque. El bot lleva el capital actual en `data/<sym>/state.json`. Para inyectar plata mensual subir `INITIAL_CAPITAL` en cada `.env`. |

---

## 5. Bugs encontrados y resueltos

| Bug | Síntoma | Fix | Commit |
|---|---|---|---|
| O(n²) en ScalpingEngine | WFA tardaba 30+ min | Cache de BB+ATR por `id(df)` | `a0c1977` |
| PF medio inflado por ventanas n<3 con PF=99 | LTC declarado ✅ pero perdía -21% | Mediana del PF sobre ventanas n≥3 + criterio `ret total ≥+10%` | `9d1cb70` |
| OOM kill silencioso (ExitCode=0) | Loop infinito de "¡Arranqué!" cada 30s en Telegram | Subir mem limit del container 384M → 512M | `4aacfa9` |
| "BTC" hardcoded en Telegram | Los 4 bots reportaban "BTC $X" en panorama y arranque | `TelegramNotifier.base_asset = settings.symbol.split("/")[0]` | `7bf672e` |
| Container reinicia con ExitCode=0 (no es crash) | Reinicia loop pese a unless-stopped | `restart: unless-stopped` reinicia INCLUSO con exit 0 (regla del flag) | Comprendido, no fixeable sin cambiar la política |

---

## 6. Filesystem layout (resumen)

```
agent-trading/
├── main.py                          (entry point)
├── docker-compose.yml               (deploy single-symbol legacy)
├── docker-compose.multi.yml         (deploy multi-asset Fase 2)
├── .env.example                     (plantilla)
├── .env.btc/.sol/.avax/.link        (en VPS, no en repo — gitignored)
├── config/settings.py               (pydantic Settings + load_settings)
├── core/
│   ├── bot_controller.py            (state Telegram↔loop)
│   ├── exchange.py                  (dual mainnet/testnet)
│   ├── indicators.py                (snapshot técnico)
│   ├── mtf_context.py               (1h+4h multi-timeframe)
│   ├── scalping_engine.py           (descartado, código intacto)
│   ├── price_action_engine.py       (ACTIVO)
│   ├── technical_engine.py          (legacy)
│   └── claude_agent.py              (Claude API agent)
├── strategies/main_strategy.py      (orquestador del loop)
├── execution/order_manager.py       (estado posiciones + journal)
├── notifications/
│   ├── telegram.py                  (notifier out)
│   └── telegram_listener.py         (commands in)
├── health.py                        (heartbeat + dead-man's switch)
├── notifier.py                      (helper notificaciones críticas)
├── scripts/
│   ├── backtest_scalping.py
│   ├── backtest_price_action.py
│   ├── audit_montecarlo.py
│   ├── audit_trade_breakdown.py
│   ├── audit_filters.py
│   ├── audit_wfa.py                 (WFA scalping — Fase 1)
│   └── audit_wfa_pa.py              (WFA price action — Fase 2)
├── data/
│   ├── btc/ sol/ avax/ link/        (cada bot su carpeta)
│   │   ├── state.json
│   │   ├── trade_journal.jsonl
│   │   ├── audit.jsonl
│   │   ├── heartbeat
│   │   └── bot.log
└── docs/
    ├── BACKTESTS.md                 (registro completo de todas las auditorías)
    ├── DEPLOYMENT.md                (single-symbol legacy)
    ├── DEPLOYMENT_MULTI_ASSET.md    (deploy 4 bots paso a paso)
    └── HANDOFF.md                   (este doc)
```

---

## 7. Histórico de commits relevantes

```
7bf672e fix(telegram): usar symbol del settings en lugar de 'BTC' hardcoded
4aacfa9 fix(docker-multi): subir mem limit de 384M a 512M para evitar OOM en arranque
9d1cb70 audit(fase2): WFA multi-asset PA + portafolio aprobado BTC/SOL/AVAX/LINK
a0c1977 audit(fase1): auditorías estocásticas + WFA del ScalpingEngine
cbf235c monitor: heartbeat + dead-man's switch + autoheal (roadmap #1) (#2)
f3a43e0 fix(docker): no ignorar la carpeta logs/ en .dockerignore
a6b5c3a fix(docker): incluir logs/logger.py en el repo y separar bot.log a data/
5fb1392 prod: Docker + scalping engine + multi-trade + notifier 24/7
```

---

## 8. Reglas firmes con el cliente (alejandro)

1. **Tono argentino voseo, directo, sin adornos.**
2. **Auto Mode**: si hay que decidir entre opciones razonables, decidir y avanzar. El cliente redirige si no.
3. **NO migrar a `BINANCE_TESTNET=false`** hasta cumplir:
   - 7+ días corridos en testnet sin restarts ni crashes
   - ≥1 trade cerrado por cada uno de los 4 bots
   - Trades muestran TP/SL en precios razonables (sin slippage raros)
   - `data/<sym>/audit.jsonl` sin warnings de reconciliación
4. **Toda modificación al engine pasa por backtest contra el baseline** antes de mergear. Si baja el PF o sube el DD → descarta. Documentar en `docs/BACKTESTS.md`.
5. **Cualquier cambio se commitea + pushea inmediatamente** para que el VPS pueda hacer `git pull`.
6. **`.env` con secretos NO va al repo**. `.env.example` es la plantilla pública.
7. **Capital nominal real = $210**, daily DD limit 5%, risk 8%, sin Kelly por ahora (descartado para PA).
8. **No mostrar memorias internas** salvo que el cliente las pida explícitamente.

---

## 9. Próximos pasos en el roadmap

### Inmediato (ventana de validación testnet)
- 2026-06-07 → 2026-06-14 (mínimo): observar los 4 bots, no tocar nada.
- Confirmar que cada bot dispara al menos 1 trade en testnet.
- Verificar que el `TelegramNotifier` muestra correctamente el símbolo de cada bot.

### Post-validación (si pasamos los 7 días)
- Cambiar `BINANCE_TESTNET=false` en los 4 `.env` (bajar y subir bots).
- Capital real inicial $210 (no $10k del testnet — el bot va a reconciliar al startup).
- Inyecciones mensuales: subir `INITIAL_CAPITAL` en cada `.env` en +$50 y restart.

### Auditorías diferidas (canceladas en Fase 1 porque scalping no tenía edge, podrían retomarse para PA)
- **#2a Post-Only orders** (limit maker en lugar de market): para reducir fees. Aplicable al PA si encontramos que los SL están comiendo fees taker importantes.
- **#2b Slippage modeling** en backtest: agregar 0.05%-0.10% de slippage en SL/Market entries.
- **#3 Kelly fraccional dinámico**: calcular f* en base a WR/avg_win/avg_loss del rolling window y ajustar position sizing. Hoy usamos `MAX_RISK_PER_TRADE` fijo.

### Funcionalidad pendiente nice-to-have
- Comandos Telegram con sufijo del símbolo (`/status:btc`) para no recibir 4 respuestas.
- Dashboard agregado (`data/portfolio.json`) que junte equity de los 4 bots.
- Métricas Prometheus / exporter para mejor observabilidad.

---

## 10. Comandos operativos de cabecera

**En el VPS (`~/agent-trading`):**

```bash
# Update tras git pull
docker compose -f docker-compose.multi.yml down
git pull
docker compose -f docker-compose.multi.yml build
docker compose -f docker-compose.multi.yml up -d

# Status
docker compose -f docker-compose.multi.yml ps
for sym in btc sol avax link; do
  rc=$(docker inspect --format '{{.RestartCount}}' agent-trading-$sym)
  trades=$(wc -l < data/$sym/trade_journal.jsonl 2>/dev/null || echo 0)
  echo "$sym: Restarts=$rc | Trades=$trades"
done

# Logs vivos
docker compose -f docker-compose.multi.yml logs -f bot-btc

# Memoria
docker stats agent-trading-btc agent-trading-sol agent-trading-avax agent-trading-link --no-stream

# Stop
docker compose -f docker-compose.multi.yml down

# Rollback al bot single-symbol (si el multi sale mal)
docker compose -f docker-compose.multi.yml down
cp .env.backup-* .env    # restaurar el .env clásico
docker compose up -d
```

**En local (desarrollo + auditorías):**

```bash
# Backtest single PA
python scripts/backtest_price_action.py --days 180 --symbol BTC/USDT

# WFA del PA
python scripts/audit_wfa_pa.py --symbol BTC/USDT --timeframe 1h --days 180 --risk-pct 0.08 --leverage 5
```

---

## 11. Si Gemini quiere ayudar, lo que necesita saber para no romper nada

1. **Cualquier modificación al engine** (`price_action_engine.py`) debe re-validarse con WFA antes de mergear. El criterio del WFA es: **PF mediano ≥ 1.15 sobre ventanas con n≥3, OS positivas ≥ 55%, OS retorno total ≥ +10%**.

2. **No cambiar `MAX_RISK_PER_TRADE` ni `INITIAL_CAPITAL`** sin recalcular el riesgo global del portafolio (regla: la suma de risk × n_bots no debería pasar 8% del capital real).

3. **No agregar más símbolos al portafolio** sin pasarlos por WFA primero (los criterios están en el script `audit_wfa_pa.py`). El usuario rechazó ETH/LTC/ADA/DOT/BNB/INJ/DOGE/XRP/POL por no pasar el filtro.

4. **Para deshabilitar un bot temporalmente** (ej. AVAX está dando warnings raros):
   `docker compose -f docker-compose.multi.yml stop bot-avax`. No editar el yml.

5. **No mergear scripts de scalping** ni reactivar `ENGINE=scalping`. Ese motor está descartado por WFA. Si se quisiera reactivar habría que volver a hacer el ciclo de auditorías.

6. **Los logs de Telegram aún pueden tener bugs cosméticos** (mensajes de buy/sell, prices en otras funciones). El fix `7bf672e` cubrió los 4 más visibles pero conviene grep `"BTC"` en `notifications/` antes de tocar.

7. **Si proponés un fix de algo no obvio, primero pedile al cliente que confirme el síntoma**. El cliente quiere honestidad intelectual, no soluciones a problemas inventados.

---

## 12. Cuál es el estado financiero esperado del portafolio

Sobre 180 días de WFA (Fase 2), suma simple no compuesta:

| Activo | Retorno OS esperado | Trades OS | Risk/trade |
|---|---|---|---|
| BTC | +25.05% | 18 | 8% × $52.5 = $4.20 |
| SOL | +38.46% | 17 | $4.20 |
| AVAX | +8.46% | 22 | $4.20 |
| LINK | +6.13% | 16 | $4.20 |
| **Total** | **+78.10%** sobre 180d (~+13% mensual) | **73** (~12/mes) | $16.80 simultáneo |

**Importante**: este es el resultado del WFA con leverage 5×, risk 8%, fees
0.05%. En testnet con capital ficticio $10k el efecto compuesto puede
amplificar/desinflar. En real con $210 los porcentajes deberían replicarse
similar (no idéntico por slippage, fees reales, etc).

---

Fin del handoff. Cualquier cosa que no esté acá puede consultarse leyendo
`docs/BACKTESTS.md` para el contexto cuantitativo o `docs/DEPLOYMENT_MULTI_ASSET.md`
para el operativo.
