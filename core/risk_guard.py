"""
core/risk_guard.py
Kill-switch de drawdown: protección de capital que el límite diario no cubre.

DOS HUECOS QUE TAPA (ver conversación de diseño 2026-06-12):

1. SANGRÍA LENTA (per-bot, drawdown desde el pico):
   El `daily_drawdown_limit` se resetea cada día → un bot puede perder 9% hoy,
   9% mañana, 9% pasado y nunca disparar el corte. Este guard mide el drawdown
   contra el PICO histórico de capital (capital_peak), que NO se resetea. Si la
   caída desde el pico supera el límite → HALT (no auto-resetea: requiere reset
   manual, porque es un evento de "algo anda mal, revisá").

2. CRASH CORRELACIONADO (portafolio, entre los 3 bots):
   BTC/SOL/AVAX están muy correlacionados; en un dump van largos a la vez y
   pierden juntos. Cada bot corre en su propio contenedor sin saber del otro.
   Este guard los coordina vía archivos en un directorio compartido: cada bot
   publica su equity, y todos leen el combinado. Si el drawdown del PORTAFOLIO
   supera el límite → HALT en todos.

Diseño de concurrencia (3 procesos):
- Escrituras atómicas (tmp + os.replace) → nunca se lee un archivo a medias.
- portfolio_peak.json protegido con flock; el pico sólo sube, así que una
  carrera perdida se autocorrige en el ciclo siguiente.
- Degradación elegante: si el dir compartido no existe o falla I/O, el guard
  de portafolio se desactiva solo y loguea, sin tumbar al bot.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import fcntl  # POSIX (Linux/VPS). En otros SO degradamos sin lock.
    _HAS_FCNTL = True
except Exception:  # pragma: no cover
    _HAS_FCNTL = False

from logs.logger import logger


# ─────────────────────────────────────────
#  Lógica pura (testeable sin I/O)
# ─────────────────────────────────────────

def drawdown_from_peak(equity: float, peak: float) -> float:
    """Drawdown fraccional desde el pico. 0.0 si no hay caída o datos inválidos."""
    if peak <= 0 or equity >= peak:
        return 0.0
    return (peak - equity) / peak


def breach_reason(equity: float, peak: float, limit: float, scope: str) -> Optional[str]:
    """
    Devuelve un motivo de HALT si el drawdown desde el pico supera `limit`.
    `limit <= 0` desactiva el chequeo. None = todo OK.
    """
    if limit <= 0:
        return None
    dd = drawdown_from_peak(equity, peak)
    if dd >= limit:
        return (f"{scope} drawdown {dd:.1%} ≥ límite {limit:.0%} "
                f"(equity ${equity:,.2f} vs pico ${peak:,.2f})")
    return None


# ─────────────────────────────────────────
#  Coordinación de portafolio (archivos compartidos)
# ─────────────────────────────────────────

@dataclass
class PortfolioSnapshot:
    combined_equity: float
    combined_peak: float
    drawdown: float
    contributors: int        # cuántos bots reportaron equity


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atómico en POSIX


class PortfolioGuard:
    """
    Agregador de equity entre bots vía un directorio compartido.
    Cada bot llama publish_equity() y luego snapshot() para obtener el
    drawdown combinado del portafolio.
    """

    EQUITY_PREFIX = "equity_"
    PEAK_FILE = "portfolio_peak.json"
    STALE_SECONDS = 15 * 60  # equity más vieja que esto se ignora (bot caído)

    def __init__(self, shared_dir: str):
        self.dir = Path(shared_dir)
        self.enabled = bool(shared_dir)
        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:  # pragma: no cover
                logger.warning(f"PortfolioGuard deshabilitado (no pude crear {shared_dir}): {e}")
                self.enabled = False

    def publish_equity(self, tag: str, equity: float, peak: float) -> None:
        if not self.enabled:
            return
        try:
            _atomic_write_json(
                self.dir / f"{self.EQUITY_PREFIX}{tag}.json",
                {"tag": tag, "equity": float(equity), "peak": float(peak),
                 "ts": time.time()},
            )
        except Exception as e:  # pragma: no cover
            logger.warning(f"PortfolioGuard: no pude publicar equity de {tag}: {e}")

    def _read_all_equity(self) -> list[dict]:
        out: list[dict] = []
        now = time.time()
        for p in self.dir.glob(f"{self.EQUITY_PREFIX}*.json"):
            try:
                with open(p) as f:
                    d = json.load(f)
                if now - float(d.get("ts", 0)) <= self.STALE_SECONDS:
                    out.append(d)
            except Exception:
                continue  # archivo a medias o corrupto: lo salteamos
        return out

    def _update_peak(self, combined_equity: float) -> float:
        """Lee/actualiza el pico del portafolio de forma atómica (peak sólo sube)."""
        peak_path = self.dir / self.PEAK_FILE
        try:
            lock_path = self.dir / (self.PEAK_FILE + ".lock")
            with open(lock_path, "w") as lock:
                if _HAS_FCNTL:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    stored = 0.0
                    if peak_path.exists():
                        with open(peak_path) as f:
                            stored = float(json.load(f).get("peak", 0.0))
                    peak = max(stored, combined_equity)
                    if peak > stored:
                        _atomic_write_json(peak_path, {"peak": peak, "ts": time.time()})
                    return peak
                finally:
                    if _HAS_FCNTL:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except Exception as e:  # pragma: no cover
            logger.warning(f"PortfolioGuard: fallo al actualizar pico: {e}")
            return combined_equity

    def snapshot(self) -> Optional[PortfolioSnapshot]:
        """Equity combinada + drawdown del portafolio. None si está deshabilitado."""
        if not self.enabled:
            return None
        rows = self._read_all_equity()
        if not rows:
            return None
        combined = sum(float(r.get("equity", 0.0)) for r in rows)
        peak = self._update_peak(combined)
        return PortfolioSnapshot(
            combined_equity=combined,
            combined_peak=peak,
            drawdown=drawdown_from_peak(combined, peak),
            contributors=len(rows),
        )
