"""
tests/test_donchian_engine.py
Tests del DonchianEngine y del modelo de ejecución del backtest.
Correr con: pytest tests/test_donchian_engine.py -v
"""

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from core.donchian_engine import DonchianEngine
from scripts.backtest_donchian import (
    DonchianBacktester,
    ExecutionModel,
    PendingEntry,
)


# ─────────────────────────────────────────
#  Fixtures / helpers
# ─────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    s = Settings(
        binance_api_key="test_key",
        binance_api_secret="test_secret",
        anthropic_api_key="test_anthropic",
        telegram_bot_token="test_telegram",
        telegram_chat_id="12345",
    )
    s.dc_adx_min = 0.0  # los tests de señal no dependen del régimen salvo que lo pidan
    return s


def make_range_df(n: int = 60, base: float = 100.0, vol: float = 100.0) -> pd.DataFrame:
    """Rango lateral determinístico: close oscila ±1, high<=base+2, low>=base-2."""
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    osc = np.tile([0.5, -0.5, 1.0, -1.0], n // 4 + 1)[:n]
    close = base + osc
    return pd.DataFrame({
        "open": close - 0.2,
        "high": base + 2.0 + np.zeros(n),
        "low": base - 2.0 + np.zeros(n),
        "close": close,
        "volume": np.full(n, vol),
    }, index=idx)


def with_breakout(
    df: pd.DataFrame, direction: str = "LONG",
    vol_mult: float = 3.0,
) -> pd.DataFrame:
    """Convierte la ÚLTIMA vela en un breakout del canal con volumen."""
    df = df.copy()
    i = df.index[-1]
    base_vol = float(df["volume"].iloc[0])
    if direction == "LONG":
        df.loc[i, ["open", "high", "low", "close"]] = [100.5, 105.5, 100.0, 105.0]
    else:
        df.loc[i, ["open", "high", "low", "close"]] = [99.5, 100.0, 94.5, 95.0]
    df.loc[i, "volume"] = base_vol * vol_mult
    return df


# ─────────────────────────────────────────
#  Señales del engine
# ─────────────────────────────────────────

class TestDonchianSignals:
    def test_breakout_long_dispara_comprar(self, settings):
        df = with_breakout(make_range_df(), "LONG")
        dec = DonchianEngine(settings).analyze(df, len(df) - 1)
        assert dec.accion == "COMPRAR"
        assert dec.direction == "LONG"
        assert dec.stop_loss_pct > 0
        assert dec.take_profit_pct >= dec.stop_loss_pct * 2 * (1 - 1e-9)

    def test_breakdown_short_dispara_vender(self, settings):
        df = with_breakout(make_range_df(), "SHORT")
        dec = DonchianEngine(settings).analyze(df, len(df) - 1)
        assert dec.accion == "VENDER"
        assert dec.direction == "SHORT"

    def test_sin_breakout_espera(self, settings):
        df = make_range_df()
        dec = DonchianEngine(settings).analyze(df, len(df) - 1)
        assert dec.accion == "ESPERAR"

    def test_volumen_bajo_bloquea(self, settings):
        df = with_breakout(make_range_df(), "LONG", vol_mult=0.5)
        dec = DonchianEngine(settings).analyze(df, len(df) - 1)
        assert dec.accion == "ESPERAR"
        assert "vol" in dec.razon.lower()

    def test_filtro_adx_bloquea_mercado_lateral(self, settings):
        settings.dc_adx_min = 99.0  # umbral imposible → siempre bloquea
        df = with_breakout(make_range_df(), "LONG")
        dec = DonchianEngine(settings).analyze(df, len(df) - 1)
        assert dec.accion == "ESPERAR"
        assert "ADX" in dec.razon

    def test_breakout_debe_ser_fresco(self, settings):
        """Dos velas seguidas sobre el canal: la segunda NO re-dispara."""
        df = with_breakout(make_range_df(61), "LONG")
        # Agregar otra vela también por encima del canal
        nxt = df.index[-1] + pd.Timedelta(minutes=15)
        df.loc[nxt] = [105.2, 106.0, 104.8, 105.8, 300.0]
        dec = DonchianEngine(settings).analyze(df, len(df) - 1)
        assert dec.accion == "ESPERAR"

    def test_sin_lookahead(self, settings):
        """La decisión en la vela i no cambia si existen velas futuras en el df."""
        df_full = with_breakout(make_range_df(80), "LONG")
        i = 59
        # Mismo df truncado exactamente en i
        df_trunc = df_full.iloc[: i + 1].copy()
        eng_a, eng_b = DonchianEngine(settings), DonchianEngine(settings)
        dec_full = eng_a.analyze(df_full, i)
        dec_trunc = eng_b.analyze(df_trunc, i)
        assert dec_full.accion == dec_trunc.accion
        assert dec_full.stop_loss_pct == dec_trunc.stop_loss_pct

    def test_sl_mode_tighter_elige_el_mas_cercano(self, settings):
        df = with_breakout(make_range_df(), "LONG")
        settings.dc_sl_mode = "tighter"
        dec_t = DonchianEngine(settings).analyze(df, len(df) - 1)
        settings.dc_sl_mode = "atr"
        dec_a = DonchianEngine(settings).analyze(df, len(df) - 1)
        settings.dc_sl_mode = "channel"
        dec_c = DonchianEngine(settings).analyze(df, len(df) - 1)
        assert dec_t.accion == "COMPRAR"
        # tighter = SL más cercano al precio → riesgo mínimo entre ambos modos
        candidates = [d.stop_loss_pct for d in (dec_a, dec_c) if d.accion == "COMPRAR"]
        assert dec_t.stop_loss_pct == min(candidates)

    def test_rr_minimo_rechaza_sl_lejano(self, settings):
        """SL de canal muy lejos + TP corto → señal descartada por R:R."""
        settings.dc_sl_mode = "channel"
        settings.dc_tp_atr_mult = 0.5   # TP ridículamente corto
        df = with_breakout(make_range_df(), "LONG")
        dec = DonchianEngine(settings).analyze(df, len(df) - 1)
        assert dec.accion == "ESPERAR"
        assert "R:R" in dec.razon


# ─────────────────────────────────────────
#  Modelo de ejecución del backtest
# ─────────────────────────────────────────

class _ScriptedEngine:
    """Motor stub: devuelve decisiones pre-armadas por índice de vela."""

    def __init__(self, script: dict):
        self.script = script

    def analyze(self, df, current_idx):
        from core.donchian_engine import DonchianDecision
        if current_idx in self.script:
            direction = self.script[current_idx]
            return DonchianDecision(
                accion="COMPRAR" if direction == "LONG" else "VENDER",
                direction=direction,
                confianza=0.9,
                razon="scripted",
                stop_loss_pct=0.05,
                take_profit_pct=0.10,
            )
        return DonchianDecision(
            accion="ESPERAR", direction="LONG", confianza=0.0, razon="",
        )


def flat_df(n: int = 20, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame({
        "open": np.full(n, price),
        "high": np.full(n, price + 0.5),
        "low": np.full(n, price - 0.5),
        "close": np.full(n, price),
        "volume": np.full(n, 100.0),
    }, index=idx)


class TestExecutionModel:
    def test_slippage_siempre_adverso(self):
        x = ExecutionModel(slippage_pct=0.001)
        assert x.fill_price(100.0, "buy") == pytest.approx(100.1)
        assert x.fill_price(100.0, "sell") == pytest.approx(99.9)

    def test_entrada_se_ejecuta_en_la_vela_siguiente(self, settings):
        """Señal al close de la vela 5 → fill al OPEN de la vela 6, nunca en la 5."""
        df = flat_df(10)
        x = ExecutionModel(taker_fee_pct=0.0005, slippage_pct=0.0)
        bt = DonchianBacktester(settings, _ScriptedEngine({5: "LONG"}), x)
        bt.run(df, warmup=1)
        assert len(bt.trades) == 1  # cerrado por "Fin del backtest"
        t = bt.trades[0]
        # entry_idx guardado en bars_held: cerró en la última vela (9) − entrada (6)
        assert t.bars_held == 9 - 6
        assert t.entry_price == pytest.approx(float(df.iloc[6]["open"]))

    def test_fees_taker_en_entrada_y_salida(self, settings):
        fee = 0.0005
        df = flat_df(10)
        x = ExecutionModel(taker_fee_pct=fee, slippage_pct=0.0)
        bt = DonchianBacktester(settings, _ScriptedEngine({5: "LONG"}), x)
        res = bt.run(df, warmup=1)
        t = res.trades[0]
        expected = (t.entry_price * t.amount_base + t.exit_price * t.amount_base) * fee
        assert t.fees == pytest.approx(expected)
        # Mercado plano sin slippage: el PnL neto es exactamente −fees
        assert t.pnl_net == pytest.approx(-t.fees)
        assert res.final_equity == pytest.approx(settings.initial_capital - t.fees)

    def test_senal_opuesta_cierra_ya_y_abre_en_proxima_vela(self, settings):
        """Separación de concerns: cierre del LONG en la vela de la señal SHORT;
        la apertura SHORT recién en la vela siguiente."""
        df = flat_df(12)
        x = ExecutionModel(taker_fee_pct=0.0, slippage_pct=0.0)
        bt = DonchianBacktester(
            settings, _ScriptedEngine({3: "LONG", 7: "SHORT"}), x,
        )
        res = bt.run(df, warmup=1)
        assert len(res.trades) == 2
        first, second = res.trades
        assert first.direction == "LONG"
        assert "opuesta" in first.exit_reason.lower()
        # LONG entró al open de la vela 4 y cerró en la vela 7 (la de la señal)
        assert first.bars_held == 7 - 4
        assert second.direction == "SHORT"
        # SHORT entró al open de la vela 8 (siguiente iteración) y cerró al final (11)
        assert second.bars_held == 11 - 8
        assert second.entry_price == pytest.approx(float(df.iloc[8]["open"]))

    def test_sl_intrabar_peor_caso(self, settings):
        """Si una vela toca SL y TP a la vez, se asume el SL (conservador)."""
        df = flat_df(10)
        # Vela 7: barre todo el rango (toca SL 5% abajo y TP 10% arriba)
        i7 = df.index[7]
        df.loc[i7, "high"] = 115.0
        df.loc[i7, "low"] = 90.0
        x = ExecutionModel(taker_fee_pct=0.0, slippage_pct=0.0)
        bt = DonchianBacktester(settings, _ScriptedEngine({5: "LONG"}), x)
        res = bt.run(df, warmup=1)
        t = res.trades[0]
        assert t.exit_reason == "Stop-loss"
        assert t.exit_price == pytest.approx(t.entry_price * 0.95)
        assert t.pnl_net < 0

    def test_slippage_cost_registrado(self, settings):
        df = flat_df(10)
        x = ExecutionModel(taker_fee_pct=0.0, slippage_pct=0.001)
        bt = DonchianBacktester(settings, _ScriptedEngine({5: "LONG"}), x)
        res = bt.run(df, warmup=1)
        t = res.trades[0]
        assert t.slippage_cost > 0
        # Ida y vuelta con 10 bps adversos en mercado plano → pierde ~20 bps
        assert t.pnl_net < 0
