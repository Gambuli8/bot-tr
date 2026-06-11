#!/usr/bin/env bash
# scripts/validate_pullback.sh
# Gauntlet completo del PullbackScalpEngine en un solo comando.
# Corre backtest single-period + Walk-Forward sobre SOL y AVAX, y guarda la
# salida en logs/ para revisar. NO deployar a real si el WFA no da ✅.
#
# Uso:
#   bash scripts/validate_pullback.sh
#   SYMBOLS="SOL/USDT" DAYS=180 bash scripts/validate_pullback.sh
set -euo pipefail
cd "$(dirname "$0")/.."

SYMBOLS="${SYMBOLS:-SOL/USDT AVAX/USDT}"
DAYS="${DAYS:-180}"
LEVERAGE="${LEVERAGE:-7}"
RISK="${RISK:-0.02}"
GRID="${GRID:-quick}"
OUT="logs/pullback_validation_$(date +%Y%m%d_%H%M%S).log"

mkdir -p logs
echo "Validando PullbackScalpEngine | symbols=[$SYMBOLS] days=$DAYS lev=${LEVERAGE}x risk=$RISK" | tee "$OUT"

for SYM in $SYMBOLS; do
  echo -e "\n############################################################" | tee -a "$OUT"
  echo "##  $SYM" | tee -a "$OUT"
  echo "############################################################" | tee -a "$OUT"

  echo -e "\n=== [1/2] Backtest single-period (${DAYS}d) ===" | tee -a "$OUT"
  python scripts/backtest_pullback.py --symbol "$SYM" --days "$DAYS" \
    --leverage "$LEVERAGE" --risk-pct "$RISK" 2>&1 | tee -a "$OUT"

  echo -e "\n=== [2/2] Walk-Forward Analysis ===" | tee -a "$OUT"
  python scripts/audit_wfa_pullback.py --symbol "$SYM" --days "$DAYS" \
    --leverage "$LEVERAGE" --risk-pct "$RISK" --grid "$GRID" 2>&1 | tee -a "$OUT"
done

echo -e "\nListo. Salida completa en: $OUT"
echo "Regla: sólo pasa a testnet/real el símbolo con WFA ✅ (OS>0 y >=60% ventanas positivas)."
