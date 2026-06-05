# Estrategia y arquitectura del bot

> Último update: 2026-06-04
> Estado actual: paper trading sobre Binance testnet, datos OHLCV de mainnet.

## TL;DR del bot

Trend-following sobre BTC/USDT con confluencia multi-timeframe. Opera tanto LONG como SHORT, paper trading con capital simulado, controlado por Telegram. Estrategia validada en backtest de 30 días con **PF 2.30, WR 47.8%, max DD 2.47%, retorno +12.47%**.

## Arquitectura

```
agent-trading/
├── main.py                          # Entry point: startup checks + lanzamiento
├── config/
│   └── settings.py                  # Pydantic Settings, carga del .env
├── core/
│   ├── exchange.py                  # ccxt wrapper. Testnet para órdenes, mainnet
│   │                                 # para OHLCV. Cache MTF 5 min.
│   ├── indicators.py                # IndicatorEngine: EMA50/200, RSI, MACD,
│   │                                 # Donchian, ATR, ADX, Bollinger, volumen.
│   ├── technical_engine.py          # Motor de decisión sin Claude (modo TESTING).
│   │                                 # 6 setups (3 LONG + 3 SHORT).
│   ├── claude_agent.py              # Motor con Claude API (modo producción).
│   ├── mtf_context.py               # MTFContext + reglas de confluencia.
│   └── bot_controller.py            # Estado compartido entre listener y strategy.
├── execution/
│   └── order_manager.py             # SL/TP/trailing, persistencia, Kelly sizing.
├── strategies/
│   └── main_strategy.py             # Loop principal: snapshot → decisión → orden.
├── notifications/
│   ├── telegram.py                  # Mensajes amigables en español.
│   └── telegram_listener.py         # Listener con long polling, control remoto.
├── scripts/
│   └── backtest.py                  # Simulador histórico con flags de A/B testing.
├── tests/
│   └── test_bot_controller.py       # Tests del controller thread-safe.
├── data/                            # State, journal, audit log.
├── logs/                            # bot.log + rotación.
└── docs/                            # Este archivo + BACKTESTS.md.
```

## Filosofía de la estrategia

Trend-following clásico con **3 capas de filtrado** para reducir falsos positivos:

1. **Setup técnico** (en TF base, 15m default): Donchian breakout / Momentum MACD / Rebote sobreventa, espejados para SHORT.
2. **Filtro de régimen** (ADX): solo operar si el mercado tiene fuerza de tendencia (ADX ≥ 20). Evita los falsos breakouts típicos de mercados laterales.
3. **Confluencia macro** (MTF 1h): descartar setups que vayan contra la tendencia del TF superior. Estándar institucional.

Esto da WR alrededor de 45-50% (típico de trend-following — no es win rate alto pero el R:R compensa con creces). El edge está en **dejar correr ganadoras y cortar perdedoras rápido**.

## Setups del TechnicalEngine

| # | Setup | Condiciones LONG | Espejo SHORT |
|---|---|---|---|
| A | **Breakout Donchian** | breakout_up + vol ≥ 0.5x + RSI < 75 + ATR ≥ 0.05% | breakout_down + vol ≥ 0.5x + RSI > 25 |
| B | **Momentum MACD** | precio > EMA50 + MACD cross alcista + RSI 40-60 subiendo + vol ≥ 0.8x | precio < EMA50 + MACD bajista + RSI 40-60 cayendo |
| C | **Rebote sobreventa** | RSI < 35 girando + precio > EMA200 + ATR ≥ 0.05% | RSI > 65 girando + precio < EMA200 |

Todos cumplen además el filtro global ADX y la confluencia MTF (si activada).

## Risk management

| Parámetro | Default | Notas |
|---|---|---|
| `initial_capital` | $200 | Tamaño realista para empezar |
| `trade_reserve_pct` | 30% | Reserva intocable |
| `max_risk_per_trade` | 1.5% | Override por Kelly si activado |
| `daily_drawdown_limit` | 10% | Si se toca, bot se frena hasta el día siguiente |
| `atr_sl_multiplier` | 2.0 | SL = ATR * 2 (con piso 0.5%, techo 5%) |
| `min_risk_reward` | 2.0 | TP ≥ 2x SL |
| `trailing_activation_pct` | 2% | Activa trailing cuando profit > 2% |
| `trailing_distance_pct` | 1.5% | Trailing sigue al precio a 1.5% |
| `scaled_tp_enabled` | off | TP escalado: parcial en TP1 + breakeven (ver abajo) |
| `tp1_r_multiple` | 1.0 | TP1 a 1× la distancia del SL (R:R 1:1) |
| `tp1_size_pct` | 50% | Fracción de la posición cerrada en TP1 |
| `breakeven_after_tp1` | on | Tras TP1, mover SL al entry ("trade gratis") |

### TP escalado + breakeven shift (off por default, roadmap #1)

Al tocar **TP1** (a `tp1_r_multiple` × la distancia del SL) se cierra
`tp1_size_pct` de la posición y el SL salta a **breakeven**. El remanente corre
al TP completo (o al trailing). Asegura parte de la ganancia temprano y elimina
el riesgo de la segunda mitad. Implementado en `order_manager.maybe_take_partial_tp1`
(vivo) y `backtest.py` (`--scaled-tp`). **Pendiente de validar con backtest real**
antes de activar — ver [BACKTESTS.md](BACKTESTS.md).

### Kelly fraccionado (activo)

Cuando hay ≥10 trades cerrados, ajusta el riesgo según el edge histórico:
- `f* = win_rate - (1 - win_rate) / (avg_win/avg_loss)`
- Usamos **half-Kelly** (`f*/2`) — más conservador
- Bound: piso 0.5%, techo 2%

## Mejoras aplicadas (validadas con backtest)

Ver [BACKTESTS.md](BACKTESTS.md) para los números completos.

| Mejora | Estado | Efecto vs baseline |
|---|---|---|
| ADX ≥ 20 | ✅ activa | WR +5.8pp, PF +0.23 |
| Kelly fraccionado | ✅ activa | Marginal positivo |
| MTF Confluence (1h) | ✅ activa | **PF +0.51, DD -1pp, retorno +3.61pp** |
| Cooldown entre trades | ❌ rechazado | PF cae a 1.09 |
| Macro EMA200 | ❌ rechazado | Neutro |
| Trailing dinámico 1:1 | ⏸ pendiente | Primera implementación dio PF 1.00 (muy agresivo) |
| TP escalado + breakeven | ⏸ implementado, sin validar | Mecánica testeada; falta backtest real (Binance bloqueado en remoto) |

## Configuración recomendada (`.env`)

```bash
# Conexión
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_TESTNET=true
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Trading
TRADING_SYMBOL=BTC/USDT
TRADING_TIMEFRAME=1m         # Para ver acción ya. Producción real: 15m
INITIAL_CAPITAL=200
MAX_RISK_PER_TRADE=0.015
DAILY_DRAWDOWN_LIMIT=0.10
MIN_CLAUDE_CONFIDENCE=0.55

# Modo (testing = TechnicalEngine, sin Claude; false = Claude API)
TESTING_MODE=true
TESTING_LOOP_SECONDS=60

# Mejoras validadas
ADX_MIN_TRENDING=20
USE_KELLY_SIZING=true
REQUIRE_MTF_CONFLUENCE=true
```

## Comandos Telegram

### Informativos (también para readonly users)

| Comando | Qué hace |
|---|---|
| `/help` | Lista de comandos |
| `/status` | Capital, ganancia/pérdida, win rate, drawdown |
| `/position` | Posición abierta con P&L en vivo y dirección |
| `/pnl` | P&L 24h / 7d / total |
| `/trades [N]` | Últimas N operaciones |
| `/logs [N]` | Últimas N líneas del log |
| `/config` | Settings activos |

### Control (solo admin)

| Comando | Qué hace |
|---|---|
| `/on` o `/start` o `/resume` | Reactiva apertura de nuevas operaciones |
| `/off` o `/pause` | Para de abrir, mantiene posiciones con SL/TP activos |
| `/close` → `/close_confirm` | Cierra la posición a mercado |
| `/stop` → `/stop_confirm` | Apaga el proceso del bot |

`/close` y `/stop` también soportan botones inline para confirmar.

## Notificaciones espaciadas

El bot NO manda diagnóstico por cada ciclo. Recibís push solo cuando:

1. **Abre una operación** — texto con precio entrada, SL, TP, ganancia/pérdida esperada
2. **Cierra una operación** — texto con resultado, motivo del cierre
3. **Trailing stop activado** — cuando se asegura ganancia
4. **Cada 30 minutos** — un panorama amigable con stats + mercado + decisión actual
5. **Errores críticos** — warnings y safe mode

## Cómo correr

```bash
# Local, paper trading
./venv/Scripts/python.exe main.py

# Backtest
./venv/Scripts/python.exe scripts/backtest.py --days 30 --timeframe 15m

# Backtest con flags
./venv/Scripts/python.exe scripts/backtest.py --days 30 --timeframe 15m \
    --adx-min 20 --kelly --mtf

# Tests
./venv/Scripts/python.exe -m pytest tests/ -v
```

## Pendientes / próximos pasos

Prioridad estimada por impacto/esfuerzo:

| # | Mejora | Esfuerzo | Edge esperado |
|---|---|---|---|
| 1 | **Trailing dinámico bien calibrado** (activar al 2:1, distance = max(1%, 3×ATR)) | 1-2h | PF estable, ganadoras más grandes |
| 2 | **Breakout por cierre + SL estructural** | 1h | WR +5pp |
| 3 | **Comisiones en backtester** | 30min | Backtest más realista |
| 4 | **Manejo robusto de InsufficientFunds / API errors** | 30min | Resiliencia en producción |
| 5 | **Comando /panorama on-demand** desde Telegram | 30min | UX |
| 6 | **Switch a Claude (modo producción)** | 1h | Filtro contextual fino |
| 7 | **Funding rate arbitrage** como capa paralela | 4-6h | Rendimiento base sin riesgo direccional |

## Decisiones de diseño que vale documentar

- **Datos de mainnet, órdenes en testnet**: el testnet de Binance tiene precios cuasi-flat (ATR 0.02%), no sirven para generar señal. Leemos OHLCV de mainnet con un cliente ccxt sin credenciales (`data_exchange`), y las órdenes siguen yendo al testnet.
- **MTF cacheado 5 min**: el contexto de 1h y 4h no cambia tan rápido. Cachear evita hammear el API.
- **Cierre por señal opuesta**: si el bot tiene LONG abierto y aparece señal SHORT fuerte, **cierra el LONG primero**. La apertura del SHORT queda para el próximo ciclo (separación de concerns).
- **`/pause` no detiene el proceso**: solo evita nuevas aperturas. Las posiciones abiertas siguen con SL/TP activo. Esto permite "pausar lógicamente" sin perder el monitoreo.
- **`is_paused` persistido**: si el bot crashea con pausa activa, al reiniciar sigue pausado (no opera solo por accidente).
- **`_thread.interrupt_main()` para `/stop_confirm`**: portable Windows/Unix, levanta `KeyboardInterrupt` que ya está manejado en `run_forever`.

## Para futuros yo / Claude que vuelvan acá

Si vas a tocar la estrategia:
1. Cambiá el código.
2. Corré `python scripts/backtest.py --days 30` con y sin tu cambio.
3. Si el PF baja o el max DD sube, descartá.
4. Actualizá [BACKTESTS.md](BACKTESTS.md) con el resultado.
5. Si gana, agregá la flag al `.env` y reiniciá el bot.

**No actives nada en producción sin pasar por backtest comparativo.**
