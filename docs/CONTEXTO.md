# Contexto para continuar el proyecto (actualizado 18/09/2026, 14:00 hora argentina)

> Pegá este archivo al empezar un chat nuevo. Resume qué es el proyecto, cómo está desplegado, qué se
> probó, qué decidió el usuario y qué falta. Detalle técnico y tablas completas en `docs/RESUMEN_PROYECTO.md`.

## 1. Quién es el usuario y cómo trabajar con él

- Habla castellano rioplatense. Mensajes claros, sin jerga innecesaria.
- Todo en **hora argentina** (America/Argentina/Buenos_Aires).
- Telegram: formato argentino de números (`$76.014,1`, `+$0,24`, `−0,89%`), **emojis**, y **un mensaje por moneda**
  (no todo junto).
- Consulta a **Gemini** como "analista": suele pegar instrucciones de Gemini. Se implementan, pero siempre se
  **validan con backtest** y se le cuentan los resultados con honestidad, aunque sean malos.
- No quiere pagar TradingView: la estrategia corre dentro del bot.
- Capital real disponible aproximado: ~210 USDT. Todavía **no opera con dinero real**.
- Reglas: no ejecutar operaciones ni transferencias reales por cuenta propia; no manejar sus claves; confirmar
  antes de tocar producción real o el proyecto n8n del VPS.

## 2. Repositorio

- GitHub `Gambuli8/bot-tr`, rama de trabajo **`feat/bingx-bot`** (commit `75a831d`). **No está mergeada a
  `main`** (`main` todavía tiene el bot viejo de Binance). Pendiente: abrir PR a `main` si el usuario lo pide.
- Bot viejo archivado en la rama `legacy/binance-bot` y el tag `legacy-binance-v1`.
- Local: `C:\Users\aleja\Desktop\agent-trading` (venv en `venv/`, `venv/Scripts/python.exe -m pytest -q` → 88 tests).
- No versionado a propósito: `.env`, `data/` (estado, historial, cache de backtest), `_legacy_local/`.

## 3. Arquitectura del bot (`bot/`)

| Archivo | Qué hace |
|---|---|
| `server.py` | FastAPI; arranca todo; `/tv/health`; webhook opcional `/tv/webhook` |
| `strategy.py` | Motor de la estrategia direccional (zona 1D + Fibo 1H + diagonal 5m), mismo que el Pine |
| `scanner.py` | Cada 5 min baja velas reales de BingX y corre el motor; warm-up silencioso de ~5 días |
| `executor.py` | Controles y apertura con SL/TP (margen fijo o riesgo fijo) |
| `sizing.py` | `build_plan` (margen fijo) y `build_plan_fixed_risk` (riesgo fijo) |
| `monitor.py` | Detecta cierres, PnL real, SL faltante, posiciones desconocidas; dispara resúmenes |
| `carry.py` | **Modo carry**: captura de funding delta-neutral (spot + short) |
| `carry_paper.py` | Exchange simulado para el carry (precios/funding reales, órdenes simuladas) |
| `bingx.py` | Cliente REST BingX (firma HMAC verificada contra ccxt), futuros, spot, transferencias |
| `narrator.py`, `fmt.py`, `telegram.py` | Mensajes y comandos de Telegram |
| `reports.py`, `drive.py`, `google_auth.py` | Resúmenes semanales/mensuales y subida a Google Drive |
| `cli.py` | `check`, `replay`, `test-signal` |

Scripts: `scripts/backtest.py` (estrategia direccional, 2 años, IS/OOS, selección de pares) y
`scripts/backtest_funding.py` (carry). TradingView: `tradingview/bingx_fibo_mtf.pine` (solo visual, nunca compilado).

Comandos de Telegram: `/estado` (general + uno por moneda), `/hoy`, `/semana`, `/mes`, `/pausa`, `/reanudar`,
`/cerrar BTC si`, `/carry`, `/carry cerrar BTC si`, `/carry cerrar todo si`, `/ayuda`.

## 4. Despliegue actual (VPS)

- VPS Contabo `root@13.140.143.209`, key `~/.ssh/agent_trading_vps`, repo en `/root/agent-trading`
  (rama `feat/bingx-bot`). El reloj del host está en CEST, no en UTC.
- `docker compose up -d --build`; containers `bingx-bot` y `bingx-autoheal`; datos en `data/bingx/`.
- Webhook público: `https://ggambuli-n8n.online/tv/*` vía el Caddy del proyecto **n8n-financiero**
  (`/root/n8n-financiero/Caddyfile`, backup `Caddyfile.backup-20260916`). No tocar n8n sin confirmar.
- `.env` del VPS (sin secretos): `BINGX_MODE=demo`, 6 pares (BTC ETH SOL XRP ZEC DOGE), `MARGIN_PER_TRADE_USDT=2`,
  `MIN_RR=1.5`, `SL_MODE`/`FILTER_TREND`/`SIZING_MODE` por defecto (fib, sin filtro, margen),
  `CARRY_ENABLED=true`, `CARRY_SYMBOLS=BTC,ETH,DOGE,XRP`, `CARRY_CAPITAL_USDT=200`, `CARRY_LEVERAGE=2`,
  `CARRY_PAPER` automático (= simulado en demo). `GOOGLE_REFRESH_TOKEN` **vacío** (Drive sin configurar).
- API key BingX con permisos: futuros, spot y Universal Transfer; **sin retiros**; restringida a la IP del VPS.

### Estado al 18/09 14:00 (AR)
- **Direccional (demo, reglas originales):** **primera operación cerrada** — XRP LONG, entrada 16/09 20:05 a
  1,2879, salida por **TP** el 17/09 11:25 a 1,3177: **+0,0852 USDT** (R:R real 4,85). Sin posiciones abiertas;
  5 setups en seguimiento esperando el retroceso al 0,618.
- **Carry SIMULADO:** los 4 pares siguen activos desde el 16/09 23:21 (BTC 0,0004 · ETH 0,01 · DOGE 411 ·
  XRP 25, 50 USDT c/u). **20 cobros de funding** (cada 8 h: 05:00, 13:00 y 21:00 AR), +0,0436 USDT en total,
  contra 0,1806 de comisiones de entrada → neto −0,137 por ahora (las comisiones se recuperan en ~8 días).
  La cobertura funciona: el precio subió ~6 % y el capital por par sigue en 49,91–49,97 de 50.
- 95,82 VST quedaron varados en el spot de la demo por un bug ya corregido (irrelevante, es dinero de prueba).

## 5. Qué se probó y qué dio (conclusiones clave)

**Estrategia direccional (zona 1D + Fibo 1H + diagonal 5m): NO rentable en ninguna variante.**
- Reglas actuales (SL Fibo 0,786, R:R ≥ 1,5): 2 años, 20 pares, ~2.700 operaciones → −0,12 R (IS) y
  −0,21 R (OOS, t −3,8). Pierde con evidencia fuerte.
- SL estructural + EMA50, break-even, parcial, filtro de impulso, filtro de volumen: nada es positivo en IS y OOS
  a la vez sobre 20 pares. El volumen mejora solo en los últimos meses (dependencia de régimen).
- Cerrojo R:R 1:4 (pedido de Gemini): con SL estructural → 0 operaciones (geométricamente imposible);
  con SL 0,786 → 402 ops, **11 % de acierto, −57,61 USDT**.
- Coincide con investigaciones anteriores del repo (bot Binance): ninguna estrategia direccional pasó.

**Carry / captura de funding: la única positiva.**
- BingX, BTC ETH SOL XRP DOGE XLM, feb-2023→sep-2026: siempre dentro ×2 **+7,3 %/año**, caída máx. 0,26 %,
  98 % meses positivos. Por año: 2023 +5,7 · 2024 +13,5 · 2025 +4,9 · 2026 +2,0 (rendimiento bajando).
- Estrés con Binance incluyendo FTX (2022): ×2 +2,7 %/año, peor mes −3,8 % (SOL).
- Apalancamiento: ×5 casi igual con más liquidaciones; **×10 empeora** (BingX +3,6–4,1 %, Binance −3,0 %/año).
- Pares: BTC/ETH ~+5,5 % (más estables); DOGE/XRP más funding (+8,7–10,3 %); SOL cayó −27 % con FTX.
  Elegidos: **BTC, ETH, DOGE, XRP a ×2**.
- Entrar/salir según el funding pierde por comisiones: conviene "siempre dentro".
- En plata: sobre 210 USDT ≈ 4–17 USDT por año según el régimen. Robusta pero de bajo rendimiento; principal
  riesgo no modelado: custodia en BingX.

## 6. Detalles técnicos que cuestan redescubrir

- BingX: klines máx. 1000 por pedido; **5m solo ~45 días de historia** (el backtest usa Binance Futures);
  funding con historia desde 2021-12 (BTC).
- Comisiones de la cuenta: spot 0,10 % (se descuenta **en la moneda comprada**), perp 0,05 % taker / 0,02 % maker.
- Mínimos reales: BTC 0,0001; **ETH 0,01 (~24 USDT)**, en demo 0,001.
- Demo (VST): transferencias funcionan con asset `VST`, pero **spot no opera con VST** → carry real imposible en
  demo; por eso existe `carry_paper.py`.
- Endpoints (verificados con ccxt): transferencia `POST /openApi/api/asset/v1/transfer`
  (`fromAccount`/`toAccount`: `spot` | `USDTMPerp`), orden spot `POST /openApi/spot/v1/trade/order`,
  margen aislado `POST /openApi/swap/v2/trade/positionMargin`, income `GET /openApi/swap/v2/user/income`.
- Modo one-way: un SHORT del carry y un LONG direccional en el mismo par se anulan → con carry REAL los pares del
  carry salen de la estrategia direccional; en simulado no.
- Windows/Git Bash: heredocs con emojis o comillas raras fallan; escribir scripts de parche con la herramienta de
  archivos y correrlos con `PYTHONIOENCODING=utf-8`.
- No correr scripts pesados con `docker exec` dentro del container en producción (límite 256 MB).

## 7. Pendientes y próximos pasos

1. **Observar el carry simulado unos días** (`/carry`): que el funding se acredite cada 8 h, que la contabilidad
   cierre y que los mensajes se entiendan.
2. Si va bien → decidir **carry real con montos mínimos** (p. ej. 50–100 USDT en 2 pares): `CARRY_PAPER=false`
   con cuenta real (implica `BINGX_MODE=live` o separar el carry del bot demo) — **decisión del usuario**.
3. Configurar Google Drive (`GOOGLE_REFRESH_TOKEN` vía `python -m bot.google_auth`).
4. Opcional: PR `feat/bingx-bot` → `main`.
5. La estrategia direccional sigue corriendo en demo solo para validar el sistema; **no pasar a real**.

Hecho el 18/09: el funding del carry entra en los resúmenes semanales y mensuales (commit `75a831d`). Cada
cobro, comisión y PnL de ajuste queda como evento `carry_income` con la hora del exchange; el resumen de
Telegram y el informe de Drive suman una sección de carry (por par y total) y el total combinado. El historial
previo se rellenó a mano en el VPS (28 eventos, backup en `data/bingx/events.jsonl.bak-20260918`).
