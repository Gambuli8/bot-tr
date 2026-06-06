"""
logs/logger.py
Wrapper sobre loguru. Expone:
  - logger: instancia global lista para usar
  - setup_logger(level): configura sinks (stdout + archivo rotado)
"""

import sys
from pathlib import Path
from loguru import logger

# El archivo de log es runtime data → va en data/, NO en logs/.
# Si dejábamos bot.log dentro de logs/, el volumen Docker (./logs:/app/logs)
# sobrescribía la carpeta y rompía el import de logs.logger. Bug arquitectural.
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_LOG_FILE = _DATA_DIR / "bot.log"


def setup_logger(level: str = "INFO") -> None:
    # En Windows la consola por defecto es cp1252 y los emojis revientan
    # con UnicodeEncodeError. Forzamos utf-8 si el stream lo soporta.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    logger.remove()
    logger.add(
        sys.stdout,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
    )
    logger.add(
        _LOG_FILE,
        level=level,
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
    )


__all__ = ["logger", "setup_logger"]
