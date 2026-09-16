"""
Persistencia en disco (data/):
  state.json    → estado vivo: pausa, trades abiertos, ids de señales vistas, reportes enviados.
  trades.jsonl  → un registro por trade cerrado (base de los resúmenes).
  events.jsonl  → todo lo que pasó (señales, rechazos, errores) para auditar.

Escritura atómica (tmp + replace) para no corromper el estado si se corta la luz.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

MAX_SEEN_IDS = 500


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.jsonl"
        self.events_path = self.dir / "events.jsonl"
        self.heartbeat_path = self.dir / "heartbeat"
        self._lock = threading.RLock()
        self.state = self._load()

    # ───────── estado ─────────

    def _load(self) -> dict:
        default = {"paused": False, "open_trades": {}, "seen_ids": [], "setups": {},
                   "reports": {}, "alerts": {}}
        if not self.state_path.exists():
            return default
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = self.state_path.with_suffix(f".corrupt-{int(time.time())}")
            self.state_path.replace(backup)
            return default
        default.update(data)
        return default

    def save(self) -> None:
        with self._lock:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)

    def update(self, **changes: Any) -> None:
        with self._lock:
            self.state.update(changes)
            self.save()

    @property
    def paused(self) -> bool:
        return bool(self.state.get("paused"))

    def mark_seen(self, signal_key: str) -> bool:
        """True si es nuevo; False si ya se procesó (TradingView a veces reenvía)."""
        with self._lock:
            seen: list = self.state.setdefault("seen_ids", [])
            if signal_key in seen:
                return False
            seen.append(signal_key)
            del seen[:-MAX_SEEN_IDS]
            self.save()
            return True

    # ───────── setups en análisis (para /estado) ─────────

    def set_setup(self, setup_id: str, info: Optional[dict]) -> None:
        with self._lock:
            setups = self.state.setdefault("setups", {})
            if info is None:
                setups.pop(setup_id, None)
            else:
                setups[setup_id] = info
            # Limpieza: setups sin novedades hace más de 3 días.
            cutoff = time.time() - 3 * 86400
            for key in [k for k, v in setups.items() if v.get("updated", 0) < cutoff]:
                setups.pop(key, None)
            self.save()

    # ───────── trades abiertos ─────────

    def open_trade(self, symbol: str, record: dict) -> None:
        with self._lock:
            self.state.setdefault("open_trades", {})[symbol] = record
            self.save()

    def pop_open_trade(self, symbol: str) -> Optional[dict]:
        with self._lock:
            rec = self.state.setdefault("open_trades", {}).pop(symbol, None)
            self.save()
            return rec

    @property
    def open_trades(self) -> dict:
        return self.state.setdefault("open_trades", {})

    # ───────── journals ─────────

    def _append(self, path: Path, record: dict) -> None:
        record = {"ts": int(time.time() * 1000), **record}
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_event(self, kind: str, **fields: Any) -> None:
        self._append(self.events_path, {"kind": kind, **fields})

    def log_closed_trade(self, record: dict) -> None:
        self._append(self.trades_path, record)

    @staticmethod
    def _read(path: Path) -> Iterable[dict]:
        if not path.exists():
            return []
        out = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def closed_trades(self, start_ms: int = 0, end_ms: Optional[int] = None) -> list[dict]:
        end_ms = end_ms or 2**62
        return [t for t in self._read(self.trades_path) if start_ms <= t.get("closed_at", 0) < end_ms]

    def events(self, start_ms: int = 0, end_ms: Optional[int] = None) -> list[dict]:
        end_ms = end_ms or 2**62
        return [e for e in self._read(self.events_path) if start_ms <= e.get("ts", 0) < end_ms]

    def beat(self) -> None:
        self.heartbeat_path.write_text(str(int(time.time())))
