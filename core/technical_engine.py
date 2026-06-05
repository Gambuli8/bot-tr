"""
core/technical_engine.py
Motor de decisión SIN Claude, basado solo en reglas técnicas duras.
Se usa en TESTING_MODE=true para ver el bot operar sin gastar API calls.

Reglas más laxas para ver actividad real:
- Compra si hay breakout reciente + tendencia alcista local + volumen
- Vende si pierde el soporte o se da reversal técnica clara
"""

from dataclasses import dataclass
from typing import Literal
from logs.logger import logger
from config.settings import Settings
from core.indicators import MarketSnapshot


@dataclass
class TechnicalDecision:
    """Misma interfaz que TradeDecision pero generada por reglas técnicas."""
    accion: Literal["COMPRAR", "VENDER", "ESPERAR"]
    confianza: float
    razon: str
    stop_loss_pct: float
    take_profit_pct: float
    advertencias: list[str]
    # LONG = abrir compra al alza ; SHORT = abrir venta al baja
    direction: Literal["LONG", "SHORT"] = "LONG"


class TechnicalEngine:
    """
    Decisor técnico sin Claude. Más agresivo y veloz.
    Diseñado para timeframes cortos en modo testing.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        logger.info("TechnicalEngine inicializado (modo TESTING — sin Claude)")

    @property
    def is_safe_mode(self) -> bool:
        # Compatibilidad con la interfaz de ClaudeAgent
        return False

    def reset_circuit_breaker(self) -> None:
        pass  # No aplica acá

    def analyze(
        self, snapshot: MarketSnapshot, trade_history=None, mtf=None
    ) -> TechnicalDecision:
        """
        Lógica de decisión basada solo en indicadores.
        Prueba primero los setups LONG; si no hay match, prueba los SHORT.
        En testing mode, las reglas son más laxas para ver actividad.
        """
        if not snapshot.is_warmed_up:
            return TechnicalDecision(
                accion="ESPERAR",
                confianza=0.0,
                razon=f"Warm-up incompleto ({snapshot.candles_available}/{self.settings.warmup_candles})",
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                advertencias=[],
            )

        # Filtro ADX: si el mercado está lateral (sin fuerza de tendencia),
        # no abrimos. Esto elimina muchos stop-loss por ruido.
        if self.settings.adx_min_trending > 0 and snapshot.adx < self.settings.adx_min_trending:
            return TechnicalDecision(
                accion="ESPERAR",
                confianza=0.2,
                razon=f"ADX {snapshot.adx:.1f} < {self.settings.adx_min_trending:.0f} (mercado sin fuerza, lateral)",
                stop_loss_pct=0.0,
                take_profit_pct=0.0,
                advertencias=[],
            )

        long_decision = self._evaluate_buy(snapshot)
        if long_decision.accion == "COMPRAR":
            if self._mtf_blocks("LONG", mtf):
                return self._wait(f"LONG bloqueado por MTF: {self._mtf_block_reason}")
            return long_decision

        short_decision = self._evaluate_short(snapshot)
        if short_decision.accion == "VENDER":
            if self._mtf_blocks("SHORT", mtf):
                return self._wait(f"SHORT bloqueado por MTF: {self._mtf_block_reason}")
            return short_decision

        # Ninguno disparó → devolvemos el mensaje más informativo
        return long_decision

    _mtf_block_reason: str = ""

    def _mtf_blocks(self, direction: str, mtf) -> bool:
        """Devuelve True si MTF está activado y bloquea el setup."""
        if mtf is None or not self.settings.require_mtf_confluence:
            return False
        from core.mtf_context import confluence_allows
        allowed, reason = confluence_allows(direction, mtf, strict_4h=False)
        if not allowed:
            self._mtf_block_reason = reason
            return True
        return False

    def _wait(self, reason: str) -> TechnicalDecision:
        return TechnicalDecision(
            accion="ESPERAR",
            confianza=0.2,
            razon=reason,
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            advertencias=[],
        )

    def _evaluate_buy(self, s: MarketSnapshot) -> TechnicalDecision:
        """
        Reglas de COMPRA (modo testing — laxas para generar actividad):

        SETUP A: Breakout clásico de Donchian
        - breakout_up = True
        - Volumen >= 1.0x (en testing reducimos de 1.2x a 1.0x)
        - RSI < 75

        SETUP B: Momentum bajo bandas (entrada en pullback)
        - Precio sobre EMA50
        - RSI subiendo (RSI > RSI_prev)
        - RSI entre 40 y 60 (zona neutral en momentum)
        - MACD crossover alcista
        - Volumen normal

        SETUP C: Rebote técnico en sobreventa
        - RSI < 35 (sobrevendido)
        - RSI > RSI_prev (girando)
        - Precio sobre EMA200 (tendencia macro alcista)
        """
        reasons = []
        confidence = 0.0
        setup_name = ""

        # SETUP A: Donchian Breakout (umbrales reducidos para mercados tranquilos)
        # Si require_macro_trend, además: precio sobre EMA200 (tendencia macro OK)
        macro_ok_long = (not self.settings.require_macro_trend) or (s.price > s.ema200)
        if (s.breakout_up
                and s.volume_ratio >= 0.5
                and s.rsi < 75
                and 0.05 <= s.atr_pct <= 6.0
                and macro_ok_long):
            confidence = 0.78
            setup_name = "Breakout Donchian"
            reasons.append(f"Rompió máximo 20v (${s.donchian_high:,.2f})")
            reasons.append(f"Vol {s.volume_ratio:.2f}x")
            reasons.append(f"RSI {s.rsi:.1f}")
            return self._build_decision(s, confidence, setup_name, reasons)

        # SETUP B: Momentum con MACD
        if (s.price > s.ema50
                and s.macd_crossover
                and 40 <= s.rsi <= 60
                and s.rsi > s.rsi_prev
                and s.volume_ratio >= 0.8):
            confidence = 0.72
            setup_name = "Momentum MACD"
            reasons.append("Precio sobre EMA50")
            reasons.append("MACD cruce alcista")
            reasons.append(f"RSI subiendo ({s.rsi_prev:.1f}→{s.rsi:.1f})")
            return self._build_decision(s, confidence, setup_name, reasons)

        # SETUP C: Rebote en sobreventa
        if (s.rsi < 35
                and s.rsi > s.rsi_prev
                and s.price > s.ema200
                and s.atr_pct >= 0.05):
            confidence = 0.70
            setup_name = "Rebote sobreventa"
            reasons.append(f"RSI {s.rsi:.1f} girando al alza")
            reasons.append("Precio sobre EMA200 (tendencia macro OK)")
            return self._build_decision(s, confidence, setup_name, reasons)

        # No hay setup LONG → devolvemos ESPERAR neutro; analyze() probará SHORT después
        return TechnicalDecision(
            accion="ESPERAR",
            confianza=0.30,
            razon=self._wait_reason_long(s),
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            advertencias=[],
            direction="LONG",
        )

    def _evaluate_short(self, s: MarketSnapshot) -> TechnicalDecision:
        """
        Reglas de SHORT (espejo de LONG, mismas confidencias):

        SETUP A_short: Breakdown clásico de Donchian
        - breakout_down = True
        - Volumen >= 1.0x
        - RSI > 25 (no en sobreventa extrema)
        - ATR razonable

        SETUP B_short: Momentum bajista con MACD
        - Precio bajo EMA50
        - MACD cruce bajista (macd_line < macd_signal y antes era ≥)
        - RSI entre 40 y 60 (zona neutral, momentum girando)
        - RSI bajando
        - Volumen normal

        SETUP C_short: Reversión técnica en sobrecompra
        - RSI > 65
        - RSI < RSI_prev (girando)
        - Precio bajo EMA200 (tendencia macro bajista)
        """
        reasons: list[str] = []
        confidence = 0.0
        setup_name = ""

        # SETUP A_short: Breakdown Donchian (umbrales reducidos)
        # Si require_macro_trend, además: precio bajo EMA200 (tendencia macro bajista)
        macro_ok_short = (not self.settings.require_macro_trend) or (s.price < s.ema200)
        if (s.breakout_down
                and s.volume_ratio >= 0.5
                and s.rsi > 25
                and 0.05 <= s.atr_pct <= 6.0
                and macro_ok_short):
            confidence = 0.78
            setup_name = "Breakdown Donchian"
            reasons.append(f"Rompió mínimo 20v (${s.donchian_low:,.2f})")
            reasons.append(f"Vol {s.volume_ratio:.2f}x")
            reasons.append(f"RSI {s.rsi:.1f}")
            return self._build_decision(s, confidence, setup_name, reasons, direction="SHORT")

        # SETUP B_short: Momentum bajista con MACD
        # macd_crossover (alcista) en snapshot = True; aproximamos cross bajista como
        # macd_line < macd_signal y RSI cayendo en la zona 40-60.
        macd_bearish = s.macd_line < s.macd_signal
        if (s.price < s.ema50
                and macd_bearish
                and 40 <= s.rsi <= 60
                and s.rsi < s.rsi_prev
                and s.volume_ratio >= 0.8):
            confidence = 0.72
            setup_name = "Momentum bajista MACD"
            reasons.append("Precio bajo EMA50")
            reasons.append("MACD bajo señal")
            reasons.append(f"RSI cayendo ({s.rsi_prev:.1f}→{s.rsi:.1f})")
            return self._build_decision(s, confidence, setup_name, reasons, direction="SHORT")

        # SETUP C_short: Reversión en sobrecompra
        if (s.rsi > 65
                and s.rsi < s.rsi_prev
                and s.price < s.ema200
                and s.atr_pct >= 0.05):
            confidence = 0.70
            setup_name = "Reversión sobrecompra"
            reasons.append(f"RSI {s.rsi:.1f} girando a la baja")
            reasons.append("Precio bajo EMA200 (tendencia macro bajista)")
            return self._build_decision(s, confidence, setup_name, reasons, direction="SHORT")

        # No hay setup SHORT
        return TechnicalDecision(
            accion="ESPERAR",
            confianza=0.30,
            razon=self._wait_reason_short(s),
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            advertencias=[],
            direction="SHORT",
        )

    def _wait_reason_long(self, s: MarketSnapshot) -> str:
        r = []
        if not s.breakout_up:
            r.append(f"sin breakout up ({s.distance_to_high_pct:+.2f}%)")
        if s.volume_ratio < 1.0:
            r.append(f"vol bajo ({s.volume_ratio:.2f}x)")
        if s.rsi >= 75:
            r.append(f"RSI alto ({s.rsi:.1f})")
        if not r:
            r.append("sin condiciones LONG")
        return f"Sin setup LONG: {', '.join(r[:3])}"

    def _wait_reason_short(self, s: MarketSnapshot) -> str:
        r = []
        if not s.breakout_down:
            r.append(f"sin breakdown ({s.distance_to_low_pct:+.2f}%)")
        if s.volume_ratio < 1.0:
            r.append(f"vol bajo ({s.volume_ratio:.2f}x)")
        if s.rsi <= 25:
            r.append(f"RSI bajo ({s.rsi:.1f})")
        if not r:
            r.append("sin condiciones SHORT")
        return f"Sin setup SHORT: {', '.join(r[:3])}"

    def _build_decision(
        self, s: MarketSnapshot, confidence: float,
        setup_name: str, reasons: list[str],
        direction: Literal["LONG", "SHORT"] = "LONG",
    ) -> TechnicalDecision:
        """Construye una decisión de apertura con stops basados en ATR."""
        # Stop-loss: 2x ATR (en porcentaje del precio)
        sl_pct = (s.atr_pct * self.settings.atr_sl_multiplier) / 100
        # Take-profit: 2.5x el riesgo
        tp_pct = sl_pct * 2.5
        # Clamps de seguridad
        sl_pct = max(0.005, min(sl_pct, 0.05))    # entre 0.5% y 5%
        tp_pct = max(sl_pct * 2.0, min(tp_pct, 0.10))

        accion = "COMPRAR" if direction == "LONG" else "VENDER"
        decision = TechnicalDecision(
            accion=accion,
            confianza=confidence,
            razon=f"[{setup_name}] {' | '.join(reasons)}",
            stop_loss_pct=round(sl_pct, 4),
            take_profit_pct=round(tp_pct, 4),
            advertencias=["MODO TESTING — sin filtro de Claude"],
            direction=direction,
        )
        logger.info(
            f"🤖 Engine técnico → {accion} {direction} ({confidence:.0%}) | {setup_name}"
        )
        return decision
