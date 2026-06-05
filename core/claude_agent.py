"""
core/claude_agent.py
Agente de decisión basado en Claude API.
Estrategia: Trend Following (Donchian Breakout) + filtros contextuales.
"""

import json
from dataclasses import dataclass
from typing import Literal, Optional
import anthropic
from pydantic import BaseModel, field_validator
from logs.logger import logger
from config.settings import Settings
from core.indicators import MarketSnapshot


class TradeDecision(BaseModel):
    accion: Literal["COMPRAR", "VENDER", "ESPERAR"]
    confianza: float
    razon: str
    stop_loss_pct: float
    take_profit_pct: float
    advertencias: list[str] = []
    # Dirección de la posición a abrir (sólo aplica si accion es COMPRAR/VENDER
    # y no hay posición abierta). LONG por defecto para mantener compatibilidad
    # con Claude, que sólo opera al alta.
    direction: Literal["LONG", "SHORT"] = "LONG"

    @field_validator("confianza")
    @classmethod
    def confianza_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confianza debe ser entre 0.0 y 1.0")
        return round(v, 3)

    @field_validator("stop_loss_pct", "take_profit_pct")
    @classmethod
    def pct_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Los porcentajes no pueden ser negativos")
        return round(v, 4)


SYSTEM_PROMPT = """
Sos un trader cuantitativo especializado en TREND FOLLOWING para mercados de alta volatilidad (cripto).
Tu enfoque está inspirado en los Turtle Traders y Paul Tudor Jones: cortar pérdidas rápido, dejar correr ganancias.

## ROL
Trader sistemático que combina señales matemáticas de breakout con juicio contextual para evitar trampas.
No tomás decisiones emocionales. Priorizás preservación de capital sobre ganancias máximas.

## FILOSOFÍA CENTRAL
"La fuerza atrae más fuerza." Comprás cuando el mercado rompe máximos con confirmación, NO cuando está sobrevendido.
Tu trabajo principal es DETECTAR BREAKOUTS GENUINOS y FILTRAR FALSOS BREAKOUTS (bull traps).

## REGLAS FIJAS (nunca las rompés)
1. Solo operás LONG (compra). Nunca shorts (estamos en spot trading).
2. NO operás en tendencia BEAR salvo reversal extremadamente clara (confianza >= 0.85).
3. Stop-loss SIEMPRE definido — basado en ATR del activo.
4. Ratio R/R mínimo 1:2 (take_profit_pct >= 2 * stop_loss_pct).
5. Si volumen anormalmente bajo (volume_ratio < 0.8) en un breakout: ESPERAR (probable falso).
6. Si Bollinger width < 2%: mercado muerto, no operar.

## SEÑAL PRINCIPAL: BREAKOUT DE DONCHIAN
Entrada (COMPRAR) requiere TODAS estas condiciones:
- breakout_up = true (precio supera el máximo de las últimas 20 velas)
- Tendencia BULL (precio sobre EMA200, EMA50 sobre EMA200)
- Volumen confirmando: volume_ratio >= 1.2 (al menos 20% sobre promedio)
- RSI < 70 (no entrar en techo extremo)
- ATR razonable (atr_pct entre 0.3% y 5%, no consolidación ni pánico)

## FILTROS DE FALSO BREAKOUT
Bajá la confianza o ESPERAR si:
- breakout_up = true PERO volume_ratio < 1.0 → probable trampa, esperar confirmación
- RSI > 75 → sobrecompra extrema, riesgo de reversal
- distance_to_high_pct > 2% → ya rompió hace rato, llegás tarde
- bb_width < 2.5% → squeeze sin dirección definida
- Precio cerca de resistencia psicológica conocida (números redondos)

## SEÑALES DE SALIDA (VENDER)
- RSI > 78 con divergencia bajista
- MACD cruza fuertemente a la baja con volumen
- Precio rompe a la baja la EMA50 con volumen
(El trailing stop y el stop-loss los maneja el sistema automáticamente.)

## CÁLCULO DE STOP-LOSS Y TAKE-PROFIT
- stop_loss_pct: usá ATR_pct × 2.0 (típicamente 0.015 a 0.04)
- take_profit_pct: mínimo stop_loss_pct × 2 (R/R 1:2)
- Si la tendencia es muy fuerte (trend_strength > 0.7), podés ampliar TP a 3:1

## FORMATO DE RESPUESTA
Respondés ÚNICAMENTE con un objeto JSON válido. Sin texto previo, sin markdown, sin explicaciones extra.

{
  "accion": "COMPRAR" | "VENDER" | "ESPERAR",
  "confianza": 0.0-1.0,
  "razon": "Explicación concisa en español, máx 200 chars",
  "stop_loss_pct": 0.025,
  "take_profit_pct": 0.05,
  "advertencias": ["lista de alertas opcionales"]
}
"""


def _snapshot_to_prompt(snapshot: MarketSnapshot, trade_history: list[dict]) -> str:
    """Convierte el snapshot a un prompt estructurado para Claude."""

    history_str = ""
    if trade_history:
        last_trades = trade_history[-5:]
        history_str = "\n## ÚLTIMAS OPERACIONES\n"
        for t in last_trades:
            pnl_emoji = "🟢" if t.get("pnl", 0) > 0 else "🔴"
            history_str += (
                f"{pnl_emoji} Entrada: ${t.get('entry_price', 0):,.2f} | "
                f"Salida: ${t.get('exit_price', 0):,.2f} | "
                f"P&L: {t.get('pnl_pct', 0):.2f}% | "
                f"Razón salida: {t.get('exit_reason', '')[:60]}\n"
            )
    else:
        history_str = "\n## ÚLTIMAS OPERACIONES\nNo hay operaciones previas.\n"

    breakout_status = "✅ SÍ" if snapshot.breakout_up else "❌ NO"

    prompt = f"""
## DATOS DE MERCADO — {snapshot.symbol} ({snapshot.timeframe})

**PRECIO**
- Actual: ${snapshot.price:,.2f}
- Cambio 1h: {snapshot.price_change_1h:+.2f}%
- Cambio 24h: {snapshot.price_change_24h:+.2f}%

**🎯 DONCHIAN CHANNEL (señal principal)**
- Máximo 20 velas: ${snapshot.donchian_high:,.2f}
- Mínimo 20 velas: ${snapshot.donchian_low:,.2f}
- Medio: ${snapshot.donchian_mid:,.2f}
- ¿BREAKOUT al alza?: {breakout_status}
- Distancia al máximo: {snapshot.distance_to_high_pct:+.3f}%

**TENDENCIA**
- Tendencia: {snapshot.trend} (fuerza: {snapshot.trend_strength:.2f}/1.0)
- Precio vs EMA50: {snapshot.price_vs_ema50:+.2f}%
- Precio vs EMA200: {snapshot.price_vs_ema200:+.2f}%

**RSI (filtro anti-sobrecompra)**
- Actual: {snapshot.rsi:.1f}
- Anterior: {snapshot.rsi_prev:.1f}
- Zona: {'SOBREVENDIDO' if snapshot.rsi < 35 else 'SOBRECOMPRADO ⚠️' if snapshot.rsi > 70 else 'NEUTRAL'}

**MACD**
- Línea: {snapshot.macd_line:.4f}
- Señal: {snapshot.macd_signal:.4f}
- Histograma: {snapshot.macd_histogram:.4f}
- Cruce alcista esta vela: {'SÍ ✅' if snapshot.macd_crossover else 'NO'}

**VOLUMEN (clave para validar breakout)**
- Actual: {snapshot.volume_current:.4f} BTC
- Promedio 20 velas: {snapshot.volume_avg_20:.4f} BTC
- Ratio: {snapshot.volume_ratio:.2f}x {'🟢 CONFIRMA' if snapshot.volume_ratio >= 1.2 else '⚠️ DÉBIL' if snapshot.volume_ratio < 0.8 else 'neutral'}

**VOLATILIDAD**
- ATR: ${snapshot.atr:,.2f}
- ATR relativo: {snapshot.atr_pct:.3f}% del precio
- Bollinger width: {snapshot.bb_width:.2f}% {'(squeeze)' if snapshot.bb_width < 2.5 else '(volátil)' if snapshot.bb_width > 4 else ''}

**BOLLINGER BANDS**
- Superior: ${snapshot.bb_upper:,.2f}
- Inferior: ${snapshot.bb_lower:,.2f}
{history_str}
---
Analizá los datos y tomá la decisión. Recordá: priorizás breakouts CONFIRMADOS POR VOLUMEN en tendencia BULL.
Si hay duda → ESPERAR.
Respondé SOLO con el JSON.
"""
    return prompt.strip()


class ClaudeAgent:
    """Agente Claude con circuit breaker después de 3 fallos consecutivos."""

    CIRCUIT_BREAKER_THRESHOLD = 3

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._consecutive_failures = 0
        self._circuit_open = False
        logger.info("ClaudeAgent inicializado (estrategia: Trend Following + Donchian)")

    @property
    def is_safe_mode(self) -> bool:
        return self._circuit_open

    def analyze(
        self,
        snapshot: MarketSnapshot,
        trade_history: Optional[list[dict]] = None,
    ) -> TradeDecision:
        if self._circuit_open:
            logger.warning("⚡ Circuit breaker ABIERTO — retornando ESPERAR")
            return TradeDecision(
                accion="ESPERAR", confianza=0.0,
                razon="Circuit breaker activo. Claude no disponible.",
                stop_loss_pct=0.02, take_profit_pct=0.04,
                advertencias=["CIRCUIT BREAKER ACTIVO — revisión manual requerida"],
            )

        if not snapshot.is_warmed_up:
            return TradeDecision(
                accion="ESPERAR", confianza=0.0,
                razon=f"Warm-up incompleto ({snapshot.candles_available}/{self.settings.warmup_candles})",
                stop_loss_pct=0.02, take_profit_pct=0.04,
            )

        try:
            decision = self._call_claude(snapshot, trade_history or [])
            self._consecutive_failures = 0
            return decision
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(
                f"❌ Error Claude ({self._consecutive_failures}/{self.CIRCUIT_BREAKER_THRESHOLD}): {e}"
            )
            if self._consecutive_failures >= self.CIRCUIT_BREAKER_THRESHOLD:
                self._circuit_open = True
                logger.critical("🚨 CIRCUIT BREAKER ACTIVADO")
            return TradeDecision(
                accion="ESPERAR", confianza=0.0,
                razon=f"Error Claude: {str(e)[:100]}",
                stop_loss_pct=0.02, take_profit_pct=0.04,
                advertencias=[f"Error #{self._consecutive_failures}: {str(e)[:200]}"],
            )

    def reset_circuit_breaker(self) -> None:
        self._circuit_open = False
        self._consecutive_failures = 0
        logger.info("Circuit breaker reseteado manualmente")

    def _call_claude(
        self, snapshot: MarketSnapshot, trade_history: list[dict]
    ) -> TradeDecision:
        user_prompt = _snapshot_to_prompt(snapshot, trade_history)

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            timeout=15.0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text.strip()
        logger.debug(f"Respuesta raw Claude: {raw_text[:200]}")

        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        data = json.loads(raw_text)
        decision = TradeDecision(**data)

        logger.info(
            f"🧠 Claude decide: {decision.accion} "
            f"(confianza: {decision.confianza:.0%}) | {decision.razon}"
        )
        return decision
