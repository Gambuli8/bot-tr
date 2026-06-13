"""
tests/test_core.py
Tests unitarios para los módulos core.
Correr con: pytest tests/ -v
"""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
from config.settings import Settings
from core.indicators import IndicatorEngine, MarketSnapshot
from core.claude_agent import TradeDecision, ClaudeAgent
from execution.order_manager import OrderManager, BotState


# ─────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────

@pytest.fixture
def mock_settings():
    """
    Settings REAL (no MagicMock) con secretos dummy. Usar el objeto real en
    lugar de MagicMock(spec=Settings) evita el bug de "campo pydantic faltante"
    cada vez que se agrega un setting nuevo que algún engine consume
    (donchian_period, adx_period, cooldown_bars, etc.). Todos los defaults
    quedan presentes automáticamente.
    """
    s = Settings(
        binance_api_key="test_key",
        binance_api_secret="test_secret",
        anthropic_api_key="test_anthropic",
        telegram_bot_token="test_telegram",
        telegram_chat_id="12345",
    )
    # Overrides puntuales que algunos tests asumen.
    s.min_claude_confidence = 0.70
    s.atr_sl_multiplier = 1.5
    s.warmup_candles = 200
    return s


@pytest.fixture
def sample_ohlcv(n=300) -> pd.DataFrame:
    """DataFrame OHLCV sintético para testing."""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="15min")
    close = 95000 + np.cumsum(np.random.randn(n) * 100)
    high = close + np.abs(np.random.randn(n) * 50)
    low = close - np.abs(np.random.randn(n) * 50)
    open_ = close + np.random.randn(n) * 30
    volume = np.abs(np.random.randn(n) * 10 + 50)

    df = pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }, index=dates)
    return df


# ─────────────────────────────────────────
#  Tests: IndicatorEngine
# ─────────────────────────────────────────

class TestIndicatorEngine:

    def test_snapshot_returns_correct_type(self, mock_settings, sample_ohlcv):
        engine = IndicatorEngine(mock_settings)
        snapshot = engine.calculate(sample_ohlcv)
        assert isinstance(snapshot, MarketSnapshot)

    def test_rsi_in_valid_range(self, mock_settings, sample_ohlcv):
        engine = IndicatorEngine(mock_settings)
        snapshot = engine.calculate(sample_ohlcv)
        assert 0 <= snapshot.rsi <= 100

    def test_trend_valid_values(self, mock_settings, sample_ohlcv):
        engine = IndicatorEngine(mock_settings)
        snapshot = engine.calculate(sample_ohlcv)
        assert snapshot.trend in ("BULL", "BEAR", "LATERAL")

    def test_trend_strength_range(self, mock_settings, sample_ohlcv):
        engine = IndicatorEngine(mock_settings)
        snapshot = engine.calculate(sample_ohlcv)
        assert 0.0 <= snapshot.trend_strength <= 1.0

    def test_warmup_flag_with_enough_candles(self, mock_settings, sample_ohlcv):
        engine = IndicatorEngine(mock_settings)
        snapshot = engine.calculate(sample_ohlcv)
        # 300 velas > 200 warmup → should be warmed up
        assert snapshot.is_warmed_up is True

    def test_warmup_flag_insufficient_candles(self, mock_settings, sample_ohlcv):
        mock_settings.warmup_candles = 500  # más que las velas disponibles
        engine = IndicatorEngine(mock_settings)
        snapshot = engine.calculate(sample_ohlcv.head(100))
        assert snapshot.is_warmed_up is False

    def test_volume_ratio_positive(self, mock_settings, sample_ohlcv):
        engine = IndicatorEngine(mock_settings)
        snapshot = engine.calculate(sample_ohlcv)
        assert snapshot.volume_ratio > 0


# ─────────────────────────────────────────
#  Tests: TradeDecision validation
# ─────────────────────────────────────────

class TestTradeDecision:

    def test_valid_decision(self):
        d = TradeDecision(
            accion="COMPRAR",
            confianza=0.85,
            razon="RSI sobrevendido con MACD crossover",
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
        )
        assert d.accion == "COMPRAR"
        assert d.confianza == 0.85

    def test_invalid_confianza_raises(self):
        with pytest.raises(Exception):
            TradeDecision(
                accion="COMPRAR",
                confianza=1.5,  # inválido
                razon="test",
                stop_loss_pct=0.02,
                take_profit_pct=0.04,
            )

    def test_negative_stop_loss_raises(self):
        with pytest.raises(Exception):
            TradeDecision(
                accion="COMPRAR",
                confianza=0.8,
                razon="test",
                stop_loss_pct=-0.02,  # inválido
                take_profit_pct=0.04,
            )


# ─────────────────────────────────────────
#  Tests: ClaudeAgent circuit breaker
# ─────────────────────────────────────────

class TestClaudeAgentCircuitBreaker:

    def test_circuit_breaker_activates_after_3_failures(self, mock_settings):
        agent = ClaudeAgent(mock_settings)

        # Mockear _call_claude para que siempre falle
        agent._call_claude = MagicMock(side_effect=Exception("API timeout"))

        mock_snapshot = MagicMock()
        mock_snapshot.is_warmed_up = True

        for _ in range(3):
            decision = agent.analyze(mock_snapshot)

        assert agent.is_safe_mode is True
        assert decision.accion == "ESPERAR"

    def test_circuit_breaker_returns_esperar(self, mock_settings):
        agent = ClaudeAgent(mock_settings)
        agent._circuit_open = True

        mock_snapshot = MagicMock()
        decision = agent.analyze(mock_snapshot)

        assert decision.accion == "ESPERAR"
        assert decision.confianza == 0.0

    def test_reset_circuit_breaker(self, mock_settings):
        agent = ClaudeAgent(mock_settings)
        agent._circuit_open = True
        agent._consecutive_failures = 3

        agent.reset_circuit_breaker()

        assert agent.is_safe_mode is False
        assert agent._consecutive_failures == 0


# ─────────────────────────────────────────
#  Tests: OrderManager
# ─────────────────────────────────────────

class TestOrderManager:

    @patch("execution.order_manager.STATE_FILE")
    @patch("execution.order_manager.JOURNAL_FILE")
    def test_should_not_buy_if_stopped(self, mock_journal, mock_state, mock_settings, tmp_path):
        mock_state.__str__ = lambda s: str(tmp_path / "state.json")
        mock_journal.__str__ = lambda s: str(tmp_path / "journal.jsonl")

        # Parchear los paths directamente
        import execution.order_manager as om_module
        om_module.STATE_FILE = tmp_path / "state.json"
        om_module.JOURNAL_FILE = tmp_path / "journal.jsonl"

        om = OrderManager(mock_settings)
        om.state.is_stopped = True

        decision = MagicMock()
        decision.accion = "COMPRAR"
        decision.confianza = 0.9

        snapshot = MagicMock()

        assert om.should_buy(decision, snapshot) is False

    @patch("execution.order_manager.STATE_FILE")
    @patch("execution.order_manager.JOURNAL_FILE")
    def test_should_not_buy_low_confidence(self, mock_journal, mock_state, mock_settings, tmp_path):
        import execution.order_manager as om_module
        om_module.STATE_FILE = tmp_path / "state.json"
        om_module.JOURNAL_FILE = tmp_path / "journal.jsonl"

        om = OrderManager(mock_settings)

        decision = MagicMock()
        decision.accion = "COMPRAR"
        decision.confianza = 0.50  # por debajo del mínimo (0.70)
        decision.stop_loss_pct = 0.02
        decision.take_profit_pct = 0.04

        snapshot = MagicMock()

        assert om.should_buy(decision, snapshot) is False
