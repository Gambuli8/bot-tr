# Estrategia: Zona 1D + Fibonacci 1H + Diagonal 5m

Versión mecánica (sin interpretación) de la estrategia acordada. Todo lo marcado con ⚙️ es
configurable en el indicador de TradingView sin tocar código.

Se describe el **LONG**. El SHORT es el espejo (resistencia, mínimos, etc.).

## 1. Diario — zona de soporte

- Pivote diario: vela cuyo mínimo/máximo es el extremo de ⚙️ 3 velas a cada lado.
- Se recuerdan los ⚙️ 40 pivotes más recientes (máximos y mínimos: un soporte roto funciona como resistencia).
- **Zona válida** = nivel donde se agrupan ⚙️ ≥ 2 pivotes dentro de ⚙️ 0.5 × ATR(14) diario.
- Soporte activo = la zona válida más cercana por debajo del precio. Sólo se usan días **cerrados**.

**Etapa `zone`:** una vela de 1H toca la zona y **cierra sin perderla**.
La "desaceleración y giro" se confirma en el paso siguiente (cambio de estructura en 1H).

## 2. Una hora — cambio de tendencia + Fibonacci

- Swing 1H: pivote de ⚙️ 3 velas a cada lado.
- **Etapa `choch`:** una vela de 1H cierra por encima del **último máximo de swing** (el último máximo decreciente de la caída).
- **Impulso** = desde el mínimo marcado desde que tocó la zona (nivel 1) hasta el máximo posterior (nivel 0).
  Se sigue extendiendo mientras el precio haga máximos nuevos.
- Se cancela si: cierra 1H por debajo de la zona, o no hay cambio en ⚙️ 48 h.

## 3. Retroceso

- **Etapa `fib`:** el precio retrocede y toca el **0.618** del impulso.
- **Invalidación:** rompe el **0.75** → `cancel`. ⚙️ "Cierre 1H" (por defecto) o "Mecha".
- Se cancela si pasan ⚙️ 48 h sin retroceso + gatillo.

## 4. Cinco minutos — gatillo

- Diagonal del retroceso = recta que une los **2 últimos máximos decrecientes de 5m** posteriores
  al techo del impulso. Si todavía hay uno solo, se une el techo del impulso con ese máximo.
- **Etapa `entry`:** una vela de 5m **cierra por encima de la diagonal** (y por encima del 0.75).

## 5. Gestión

| | |
|---|---|
| Stop Loss | 0.75 − ⚙️ 0.1 × ATR(14) de 1H |
| Take Profit | techo del impulso (nivel 0) |
| Margen | fijo, 1–2 USDT (`MARGIN_PER_TRADE_USDT`) |
| Apalancamiento | el mínimo que acepte BingX para ese margen (tope `MAX_LEVERAGE`) |
| Tipo de margen | aislado: lo máximo que se pierde por operación es su margen |

## 6. Controles del bot (además de la estrategia)

El bot **no entra** (y te avisa por qué) si:

- La señal llegó con más de 180 s de atraso o el precio se movió más de 0.4 % desde la señal.
- Ya hay una operación abierta en ese par, o ya hay 3 abiertas en total.
- Las pérdidas del día llegaron a 3 USDT.
- Con el apalancamiento necesario, la **liquidación quedaría antes que el SL**.
- El **R:R neto de comisiones es menor a 1.5**.
- El bot está en pausa (`/pausa`).

## Decisiones abiertas (confirmar antes de operar real)

1. Invalidación del 0.75: ¿por **cierre de 1H** (actual) o por **mecha**?
2. ¿Hace falta además una **vela diaria de rechazo** (mecha ≥ 50 %) o alcanza con tocar la zona + cambio 1H (actual)?
3. Colchón del SL: 0.1 × ATR 1H (actual) o un nivel fijo (por ejemplo 0.786).
4. R:R mínimo 1.5 (actual).

## Sobre validar la estrategia

- **5 días en demo prueban que el sistema funciona**, no que la estrategia gane: con tres
  temporalidades alineadas va a haber pocas entradas.
- TradingView sólo carga ~1–2 meses de velas de 5m (según el plan), así que un backtest de años
  necesita replicar estas reglas en Python con históricos de BingX/Binance. Es el siguiente paso recomendado.
