"""
tests/test_choch.py
Tests del detector de CHoCH (Change of Character) del PriceActionEngine.
Usa datos sintéticos — no requiere red ni datos de mercado.

Construimos la lista de swings a mano (lo que find_swings produciría) para
probar la lógica de detect_choch con precisión, y un OHLCV mínimo donde sólo
importan las dos últimas velas (la del quiebre y la previa) y el volumen.

CHoCH LONG (4h BULL): tras un pullback que deja un Lower High, una vela cierra
por encima de ese Lower High → quiebre de la estructura correctiva → LONG.
CHoCH SHORT (4h BEAR): simétrico (Higher Low roto por abajo).
"""

import pandas as pd

from core.price_action_engine import Swing, detect_choch


N = 30  # velas (≥ vol_window + 5)


def _build_df(prev_close, last_close, last_vol=1000.0, base_vol=1000.0):
    """OHLCV de N velas. Las velas controladas son la última (quiebre) y la previa."""
    idx = pd.date_range("2026-01-01", periods=N, freq="1h")
    closes = [100.0] * N
    closes[-2] = prev_close
    closes[-1] = last_close
    vols = [base_vol] * N
    vols[-1] = last_vol
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": vols,
        },
        index=idx,
    )
    return df, idx


def _swing(idx, i, kind, price):
    return Swing(ts=idx[i], idx=i, kind=kind, price=price)


def _bull_swings(idx, last_sh=106.0, prev_sh=110.0, last_sl=102.0, prev_sl=100.0):
    """prev_sh (HH) → low → last_sh (Lower High) → last_sl (dip de la corrección)."""
    return [
        _swing(idx, 5, "high", prev_sh),
        _swing(idx, 10, "low", prev_sl),
        _swing(idx, 18, "high", last_sh),
        _swing(idx, 24, "low", last_sl),
    ]


def _bear_swings(idx, last_sl=94.0, prev_sl=90.0, last_sh=98.0, prev_sh=100.0):
    """prev_sl (LL) → high → last_sl (Higher Low) → last_sh (techo de la corrección)."""
    return [
        _swing(idx, 5, "low", prev_sl),
        _swing(idx, 10, "high", prev_sh),
        _swing(idx, 18, "low", last_sl),
        _swing(idx, 24, "high", last_sh),
    ]


def test_choch_long_dispara_en_quiebre_de_lower_high():
    """4h BULL + Lower High + cierre por encima → LONG."""
    df, idx = _build_df(prev_close=104.0, last_close=108.0)  # 108 > 106 (LH), 104 ≤ 106
    sig = detect_choch(df, _bull_swings(idx), "BULL", require_vol=False)
    assert sig is not None, "Debería detectar CHoCH alcista"
    assert sig.direction == "LONG"
    assert sig.broken_level == 106.0
    assert sig.protective_level == 102.0
    assert sig.broken_level < sig.candle_close
    assert sig.protective_level < sig.broken_level


def test_choch_short_dispara_en_quiebre_de_higher_low():
    """4h BEAR + Higher Low + cierre por debajo → SHORT."""
    df, idx = _build_df(prev_close=96.0, last_close=92.0)  # 92 < 94 (HL), 96 ≥ 94
    sig = detect_choch(df, _bear_swings(idx), "BEAR", require_vol=False)
    assert sig is not None, "Debería detectar CHoCH bajista"
    assert sig.direction == "SHORT"
    assert sig.broken_level == 94.0
    assert sig.protective_level == 98.0
    assert sig.broken_level > sig.candle_close
    assert sig.protective_level > sig.broken_level


def test_choch_no_dispara_si_no_hay_ruptura():
    """La última vela NO cierra sobre el lower high."""
    df, idx = _build_df(prev_close=104.0, last_close=105.0)  # 105 < 106
    assert detect_choch(df, _bull_swings(idx), "BULL", require_vol=False) is None


def test_choch_no_redispara_si_la_previa_ya_habia_roto():
    """Sólo dispara en la vela del quiebre, no en las siguientes."""
    df, idx = _build_df(prev_close=107.0, last_close=109.0)  # previa ya > 106
    assert detect_choch(df, _bull_swings(idx), "BULL", require_vol=False) is None


def test_choch_respeta_estructura_macro():
    """Estructura RANGE en 4h ⇒ nunca opera."""
    df, idx = _build_df(prev_close=104.0, last_close=108.0)
    assert detect_choch(df, _bull_swings(idx), "RANGE", require_vol=False) is None


def test_choch_long_requiere_lower_high():
    """Si el último swing high es Higher High (no hubo corrección), no dispara."""
    df, idx = _build_df(prev_close=104.0, last_close=120.0)
    swings = _bull_swings(idx, last_sh=115.0, prev_sh=110.0)  # 115 > 110 → Higher High
    assert detect_choch(df, swings, "BULL", require_vol=False) is None


def test_choch_require_vol_descarta_volumen_bajo():
    """Con require_vol, una ruptura sin volumen institucional se descarta."""
    df, idx = _build_df(prev_close=104.0, last_close=108.0, last_vol=1000.0)  # ratio ~1.0
    swings = _bull_swings(idx)
    assert detect_choch(df, swings, "BULL", require_vol=True) is None
    assert detect_choch(df, swings, "BULL", require_vol=False) is not None
    # Con volumen alto (2x), pasa incluso exigiendo volumen
    df_hi, idx_hi = _build_df(prev_close=104.0, last_close=108.0, last_vol=2000.0)
    assert detect_choch(df_hi, _bull_swings(idx_hi), "BULL", require_vol=True) is not None
