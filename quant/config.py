"""Constantes del pipeline.

Los límites de riesgo están escritos acá a propósito, como constantes de módulo: NO se leen de variables de
entorno, archivos ni de ningún modelo. Cambiarlos exige editar código, hacer commit y volver a desplegar.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Mercado ────────────────────────────────────────────────────────────────────────────────────────────
SYMBOL = "BTC/USDT"
TIMEFRAME = "4h"
BAR_MS = 4 * 60 * 60 * 1000
BARS_PER_YEAR = 6 * 365
HISTORY_YEARS = 4

# Host de datos públicos de Binance. Desde Argentina/VPS sirve api.binance.com; desde nubes de EE.UU. Binance
# bloquea la IP ("restricted location") y hay que usar el espejo oficial de market data. No hace falta API key.
BINANCE_DATA_HOST = os.environ.get("BINANCE_DATA_HOST", "data-api.binance.vision")

# ── Costos (cuenta spot Binance estándar, sin descuento BNB) ──────────────────────────────────────────────
FEE_RATE = 0.0010          # 0,10 % por lado (taker). Con BNB sería 0,075 %: usamos el peor caso.
SLIPPAGE_RATE = 0.0005     # 5 pb por lado sobre el precio de ejecución (BTC spot con <1k USDT: conservador)
FUNDING_RATE_8H = 0.0      # spot no paga funding. Se mantiene explícito: el motor lo descuenta igual.
MIN_NOTIONAL_USDT = 5.0    # mínimo de orden de Binance para BTC/USDT
QTY_STEP = 0.00001         # stepSize de BTC/USDT

# ── Riesgo: RÍGIDO. No tocar sin re-correr el research completo. ───────────────────────────────────────
INITIAL_CAPITAL_USDT = 1000.0
RISK_PER_TRADE = 0.01          # 1 % del equity entre la entrada y el stop
MAX_DAILY_LOSS = 0.03          # kill-switch diario: −3 % desde el equity de las 00:00 UTC (incluye no realizado)
MAX_DRAWDOWN_HALT = 0.15       # −15 % desde el máximo de equity → bot apagado hasta reset manual
MAX_POSITION_PCT = 0.95        # spot sin apalancamiento: el nocional nunca supera el 95 % del equity
MIN_STOP_DIST_PCT = 0.003      # un stop a <0,3 % es ruido + costos: se rechaza la entrada
MAX_STOP_DIST_PCT = 0.20       # un stop a >20 % dejaría la posición en tamaño irrelevante: se rechaza
MAX_DATA_STALENESS_BARS = 2    # si la última vela cerrada tiene más de 2 barras de atraso, no se abren entradas

# ── Validación (los umbrales se fijan ANTES de ver resultados; no se mueven después) ────────────────────
WF_TRAIN_BARS = BARS_PER_YEAR          # 12 meses de entrenamiento
WF_TEST_BARS = BARS_PER_YEAR // 4      # 3 meses fuera de muestra
MIN_TRAIN_TRADES = 8
GATES = {
    "grid_positive_frac": 0.60,   # ≥60 % de las combinaciones de parámetros con beneficio neto > 0
    "oos_net_return": 0.0,        # walk-forward: retorno neto OOS encadenado > 0
    "oos_profit_factor": 1.10,
    "oos_min_trades": 25,
    "wf_efficiency": 0.30,        # retorno anual OOS / retorno anual IS
    "mc_prob_loss": 0.10,         # bootstrap de trades OOS: P(retorno final ≤ 0) < 10 %
    "mc_dd95": 0.25,              # percentil 95 del drawdown máximo < 25 %
    "cost_x2_net_return": 0.0,    # OOS sigue positivo con comisiones y slippage duplicados
}
MC_RUNS = 5000
SEED = 42

# ── Rutas ───────────────────────────────────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("QUANT_DATA_DIR", Path(__file__).resolve().parent.parent / "data" / "quant"))
DB_PATH = DATA_DIR / "ohlcv.sqlite"
VALIDATED_PATH = DATA_DIR / "validated_strategy.json"
REPORT_PATH = DATA_DIR / "research_report.md"
STATE_PATH = DATA_DIR / "paper_state.json"
HALT_PATH = DATA_DIR / "HALTED"
LOG_PATH = DATA_DIR / "bot.log.jsonl"
