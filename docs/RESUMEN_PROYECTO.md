# Resumen del proyecto — Bot de trading BingX (al 16/09/2026, actualizado con la iteración 2)

> Documento para compartir con otro asistente (Gemini) y retomar el trabajo con contexto completo.
> Horarios en hora de Argentina (UTC−3).

## 1. Objetivo

Bot automático para **Futuros Perpetuos USDT-M de BingX** que opera con **margen fijo de 1–2 USDT** por
operación. Plan: **5 días en cuenta demo** (saldo virtual VST) y, si todo funciona, pasar a dinero real
con 1–2 USDT por operación. Me tiene que avisar por **Telegram** todo lo que hace (análisis, entradas,
salidas) en lenguaje simple, y generar **resúmenes semanales y mensuales** que se guarden en **Google Drive**.

## 2. Qué se hizo con el bot anterior

- El bot viejo (Binance, estrategia de liquidity sweeps) se **archivó** en la rama `legacy/binance-bot`
  y el tag `legacy-binance-v1` del repo `Gambuli8/bot-tr`. Sus containers en el VPS están apagados.
- De ese bot sólo se reutilizaron lecciones que ya habían costado plata: cierres siempre `reduceOnly`
  (un cierre nunca puede abrir una posición opuesta), no dejar nunca una posición sin SL, detectar
  posiciones que el bot no conoce, y escapar caracteres en Telegram.

## 3. La estrategia (reglas mecánicas)

Se tomó la idea original (zona diaria + Fibonacci 1H + diagonal 5m) y se convirtió en reglas exactas.
LONG (SHORT es el espejo):

1. **1D – Zona:** niveles donde se agrupan ≥ 2 pivotes diarios (3 velas a cada lado) dentro de
   ±0,5 × ATR(14) diario. Sólo días cerrados. Etapa "zona": una vela de 1H toca la zona y cierra sin perderla.
2. **1H – Cambio de tendencia:** cierre de 1H por encima del último máximo de swing (3 velas a cada lado).
   El impulso va desde el mínimo desde que tocó la zona (nivel 1) hasta el máximo posterior (nivel 0).
3. **Retroceso:** el precio toca el **0,618**. Se cancela si una vela de 1H **cierra** más allá del **0,75**
   (las mechas no cancelan), si pasan 48 h, o si el precio vuelve al techo del impulso sin gatillar.
4. **5m – Gatillo:** diagonal que une los 2 últimos máximos decrecientes del retroceso; entrada cuando
   una vela de 5m cierra por encima.
5. **Gestión:** SL fijo en **Fibo 0,786**, TP en el **techo del impulso**, R:R mínimo **1,5** neto de comisiones.

Decisiones tomadas (con Gemini): invalidación por cierre 1H, sin vela diaria de rechazo, SL fijo 0,786, R:R ≥ 1,5.

## 4. Arquitectura actual

```
VPS (Docker) · container bingx-bot
  ├─ scanner (cada 5 min, tras el cierre de vela): baja velas 1D/1H/5m del mercado real de BingX
  │     → motor de estrategia en Python (bot/strategy.py)
  │     → eventos: zona → cambio 1H → 0,618 → entrada (o cancelado)
  ├─ ejecutor: controles → sizing → orden a mercado con SL y TP adjuntos en BingX
  ├─ monitor (cada 20 s): detecta cierres por TP/SL, calcula el PnL real, repone SL faltantes
  ├─ Telegram: narración + comandos
  └─ resúmenes semanales/mensuales → Telegram + Google Drive
```

- **TradingView ya no es necesario:** los webhooks exigen plan pago, así que la estrategia se programó
  dentro del bot (gratis, sin límite de alertas, y permite backtest). El indicador Pine
  (`tradingview/bingx_fibo_mtf.pine`) tiene la misma lógica y queda para **ver** los setups en el gráfico
  (funciona en el plan gratis). El webhook de TradingView sigue disponible como opción (`STRATEGY_SOURCE=tradingview`).
- **Sizing con margen fijo:** el apalancamiento es el mínimo que acepta BingX para ese margen
  (BTC ×4 con 2 USDT). Margen aislado. Se rechaza la entrada si la liquidación quedaría antes que el SL
  o si el R:R neto < 1,5.
- **Controles:** señales duplicadas, bot en pausa, señal con más de 180 s de atraso, precio movido
  > 0,4 %, ya hay posición en el par, máximo 3 posiciones, límite de pérdida diaria de 3 USDT.
- **Seguridad:** API key de BingX sin permiso de retiro y restringida a la IP del VPS; cuenta en modo
  unidireccional; webhook con clave secreta y sólo IPs de TradingView.

## 5. Telegram

- Mensajes en castellano simple, formato argentino (`$76.014,1`, `+$0,24`, `−0,89 %`) y emojis.
- Avisa cada etapa del análisis, cada entrada (margen, apalancamiento, SL/TP con pérdida máxima y ganancia)
  y cada cierre (resultado, comisiones, duración, acumulado del día).
- Comandos: `/estado` (mensaje general + uno por moneda), `/hoy`, `/semana`, `/mes`, `/pausa`,
  `/reanudar`, `/cerrar BTC`.

## 6. Estado actual

- **Funcionando en demo** desde el 16/09/2026 con 6 pares: BTC, ETH, SOL, XRP, ZEC, DOGE.
- Verificado: firma de la API, saldo demo 100.000 VST, modo one-way, ciclo del scanner cada 5 min,
  endpoint de salud por HTTPS.
- 65 tests automáticos.
- **Pendiente:**
  - Probar una operación completa en demo (entrada → `/estado` → `/cerrar`).
  - `GOOGLE_REFRESH_TOKEN` para Drive (hoy los resúmenes llegan sólo por Telegram).
  - Compilar el Pine en TradingView (nunca se probó el compilador).

## 7. Resultados de la estrategia (backtest)

Backtest con el **mismo motor del bot**, 12 meses (15/09/2025 → 16/09/2026), 6 pares, velas de 5m de
Binance Futures (BingX sólo guarda ~45 días de 5m; los precios son prácticamente iguales).
Simulación conservadora: entrada al cierre + 0,03 % de slippage, comisiones taker, si SL y TP caen en la
misma vela cuenta SL, 1 posición por par, máx. 3 abiertas, límite diario de 3 USDT, mínimos de contrato reales.

**Anti-sobreajuste:** 81 variantes. Se eligen con los primeros 8 meses (*in-sample*, IS) y se validan en los
últimos 4 (*out-of-sample*, OOS: 19/05 → 16/09/2026), que no se usaron para elegir.
"R" = múltiplos del riesgo de cada operación (+1 R = gané lo mismo que arriesgaba).

| Variante | Período | Ops | Acierto | R por op. | Total | Profit factor |
|---|---|---|---|---|---|---|
| **Reglas actuales** (SL 0,786 · TP impulso · R:R ≥ 1,5) | IS | 309 | 26 % | −0,01 | −3,4 R | 0,99 |
| | **OOS** | 172 | **20 %** | **−0,32** | **−55,2 R** | 0,60 |
| **SL en inicio del impulso + filtro de tendencia EMA50 diaria** | IS | 44 | 45 % | +0,30 | +13,0 R | 1,54 |
| | **OOS** | 22 | **41 %** | **+0,16** | **+3,5 R** | 1,27 |
| SL en inicio del impulso · TP 3R · sin filtro | IS | 156 | 33 % | +0,27 | +42,2 R | 1,41 |
| | OOS | 111 | 27 % | +0,02 | +1,8 R | 1,02 |
| SL en inicio del impulso · TP impulso · sin filtro | IS | 85 | 44 % | +0,24 | +20,2 R | 1,42 |
| | OOS | 34 | 26 % | −0,24 | −8,2 R | 0,67 |

**Conclusiones:**
1. **El SL en 0,786 es el principal problema:** queda muy ajustado y las mechas lo barren
   (acierto 20–26 %). Llevarlo al **inicio del impulso (nivel 1,0) − 0,1 × ATR 1H** sube el acierto a 40–45 %.
2. **Operar sólo a favor de la tendencia diaria** (LONG si el cierre diario está sobre la EMA50, SHORT si
   está debajo) es la única variante que se mantuvo claramente positiva fuera de la muestra.
3. El filtro de SL mínimo (0,3 % / 0,6 %) no cambia nada con SL estructural.
4. Por horario (hora argentina: 00–08, 08–16, 16–24 h) no hay diferencias relevantes → no conviene filtrar por hora.
5. **Advertencias:** 22 operaciones fuera de muestra es poco; +0,16 R/op no es estadísticamente
   concluyente. Con margen fijo, el resultado en USDT depende mucho de los mínimos de contrato de cada par
   (ETH real exige ×13), por eso puede dar positivo en R y levemente negativo en USDT. La frecuencia baja a
   ~5–6 operaciones por mes entre los 6 pares.


### 7.b Iteración 2 (pedido de Gemini): riesgo fijo, gestión activa, filtros nuevos, 2 años

**Qué se implementó** (todo activable por parámetro):
- `bot/sizing.py → build_plan_fixed_risk`: **riesgo fijo** por operación (0,50 USDT). Cantidad = riesgo /
  (distancia al SL + comisiones), redondeada hacia abajo; se descarta si no llega al mínimo del contrato.
  Apalancamiento = el máximo que mantiene la liquidación lejos del SL (margen mínimo).
- `bot/strategy.py → StrategyParams`: `sl_mode="structure"` (inicio del impulso − 0,1×ATR 1H),
  `filter_trend` (EMA50 diaria), `filter_impulse` (impulso ≥ 1,5×ATR 1H), `filter_volume` (vela gatillo con
  volumen > SMA20). Apagados por defecto: el bot en demo sigue con las reglas originales.
- `scripts/backtest.py`: gestión `BE` (SL a break-even + comisiones en +1R), `PARC` (cierra 50 % en +1R),
  ambas; **2 años** (26/09/2024 → 16/09/2026); OOS = últimos 8 meses (desde 19/01/2026); selección objetiva
  de **20 pares** (entre los 40 más líquidos, mejor ATR% diario en IS / spread actual de BingX); estadístico t.

**Cómo correrlo:**
```
python scripts/backtest.py --months 24 --oos-months 8 --pairs auto --n-pairs 20
python scripts/backtest.py --months 24 --oos-months 8 --pairs current
python scripts/backtest.py --pairs BTC-USDT,ETH-USDT --risk 0.5 --max-open 3 --daily-loss 3
```

**Resultados — 20 pares objetivos** (R por operación · t entre paréntesis; t ≥ 2 ≈ evidencia razonable):

| Variante | IS (16 meses) | OOS (8 meses) |
|---|---|---|
| REF — reglas actuales del bot | 1784 ops · 24 % · **−0,12 R** (t −3,0) | 913 ops · 23 % · **−0,21 R** (t −3,8) |
| BASE — SL estructural + EMA50 | 310 ops · 31 % · −0,17 R (t −2,3) | 164 ops · 33 % · −0,09 R (t −0,9) |
| BASE + BE | 310 · 25 % · −0,14 R | 171 · 22 % · −0,12 R |
| BASE + PARC | 310 · 31 % · −0,17 R | 164 · 33 % · −0,08 R |
| BASE + BE + PARC | 310 · 44 % · −0,15 R | 171 · 51 % · −0,09 R |
| BASE + IMP | 311 · 31 % · −0,17 R | 165 · 33 % · −0,10 R |
| BASE + VOL | 185 · 30 % · −0,17 R (t −1,8) | 91 · 43 % · **+0,17 R** (t +1,2) |
| BASE + VOL + BE + PARC | 185 · 46 % · −0,13 R | 94 · 59 % · +0,09 R (t +0,9) |

**Resultados — los 6 pares actuales** (mismo período):

| Variante | IS | OOS |
|---|---|---|
| REF | 724 · 24 % · −0,13 R (t −2,1) | 310 · 22 % · −0,28 R (t −3,3) |
| BASE | 86 · 28 % · −0,25 R | 41 · 37 % · +0,03 R |
| BASE + VOL | 48 · 33 % · −0,05 R | 29 · 59 % · +0,60 R (t +2,4) |
| BASE + VOL + BE + PARC | 48 · 56 % · +0,07 R (t +0,5) | 30 · 70 % · +0,29 R (t +1,7) |

**Conclusiones de la iteración 2:**
1. **Las reglas actuales pierden con evidencia fuerte**: ~2.700 operaciones en 2 años y 20 pares, negativas
   en los dos períodos (t −3 a −3,8). No es mala suerte de un mes.
2. **El SL estructural + EMA50 no alcanza**: con más datos da negativo (el +0,16 R de la iteración 1 era ruido
   de una muestra de 22 operaciones).
3. **Break-even y parcial suben mucho la tasa de acierto (hasta 50–70 %) pero no crean ventaja**: cambian
   la forma de los resultados, no el promedio.
4. **El filtro de fuerza del impulso no filtra casi nada** (casi todos los impulsos ya superan 1,5×ATR).
5. **El filtro de volumen es lo único que mejora**, pero es **negativo en IS y positivo en OOS en los dos
   universos** → patrón de dependencia de régimen (los últimos 8 meses fueron favorables), no una ventaja
   estable. En 20 pares, **ninguna variante es positiva en IS y OOS a la vez**.
6. Sólo una combinación (BASE + VOL + BE + PARC en los 6 pares actuales) es positiva en ambos períodos, con
   78 operaciones en total y t < 2: no es evidencia suficiente. Además genera ~3 operaciones por mes, así que
   juntar 100 operaciones en demo llevaría más de 2 años.
7. Riesgo fijo de 0,50 USDT: ~13 % de las entradas se descartan por no llegar al mínimo de contrato; el margen
   máximo usado a la vez fue ~7,5 USDT con 20 pares.

**Contexto de investigaciones anteriores del mismo repo (bot Binance, 2026):** liquidity sweeps 1H, breakouts
de volatilidad, IFVG y tendencia diaria multi-activo tampoco pasaron validaciones de varios años. Lo único que
se sostuvo fue **captura de funding delta-neutral** (≈ +7,6 %/año, 92 % de meses positivos, drawdown ~0,3 %).

## 8. En qué nos puede ayudar Gemini para mejorar la tasa de acierto

Queremos subir la tasa de acierto **sin sobreajustar**. Todo lo que propongas lo vamos a probar en el
backtest (8 meses para elegir, 4 meses para validar). Pedidos concretos:

1. **Revisar las reglas mecánicas contra la estrategia original** (sección 3): ¿la definición de zona
   (pivotes agrupados), el "cambio de tendencia" (cierre sobre el último swing 1H) y la diagonal de 5m
   representan bien lo que se ve en el video? ¿Qué parte simplificamos de más?
2. **Proponer filtros de confluencia como hipótesis**, cada uno con su razón de ser, para testear:
   - Tendencia de 4H además de la diaria.
   - Volumen en la vela de ruptura de la diagonal o en el cambio 1H.
   - Tamaño mínimo del impulso (por ejemplo ≥ 1,5 × ATR 1H) o distancia libre hasta la zona opuesta.
   - Régimen de volatilidad (ATR en percentiles) o evitar horarios de noticias (CPI, FOMC).
   - Divergencia de RSI al tocar la zona.
3. **Gestión de la operación:** ¿SL estructural vs. 0,786 con confirmación? ¿Mover a break-even al llegar
   a 1R? ¿Tomar parcial en 1R y dejar correr el resto al techo del impulso?
4. **Selección de pares:** ¿qué criterio objetivo (liquidez, volatilidad, spread) conviene para elegir los
   pares, en vez de elegirlos por el resultado del backtest?
5. **Riesgo:** con margen fijo de 1–2 USDT el riesgo en USDT cambia mucho entre pares por los mínimos de
   contrato. ¿Conviene pasar a riesgo fijo por operación (por ejemplo 0,10 USDT) respetando esos mínimos?
6. **Cuidar la estadística:** ¿cuántas operaciones fuera de muestra deberíamos exigir antes de pasar a real?

Lo que NO nos sirve: elegir parámetros "a ojo" sin testearlos, o sumar muchos filtros a la vez
(se sobreajusta y deja de funcionar en vivo).

