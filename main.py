"""
main.py
Entry point del bot de trading.
Valida configuración, conexión y arranca el loop principal.
"""

import sys
from pathlib import Path

# Asegurar que el root del proyecto está en el path
sys.path.insert(0, str(Path(__file__).parent))

from logs.logger import setup_logger, logger
from config.settings import load_settings
from core.bot_controller import BotController
from notifications.telegram_listener import TelegramListener
import notifier as nf


def startup_checks(settings, exchange) -> bool:
    """
    Valida todo antes de arrancar. Si algo falla, el bot NO arranca.
    Retorna True si todo está OK.
    """
    logger.info("=" * 50)
    logger.info("  BOT DE TRADING BTC/USDT — STARTUP CHECKS")
    logger.info("=" * 50)

    checks = []

    # 1. Conexión al exchange
    try:
        exchange.validate_connection()
        checks.append(("Conexión exchange", True))
    except Exception as e:
        checks.append(("Conexión exchange", False))
        logger.error(f"❌ Fallo de conexión: {e}")

    # 2. Verificar modo testnet
    if settings.binance_testnet:
        logger.info("✅ Modo TESTNET — operando con dinero ficticio")
        checks.append(("Modo testnet", True))
    else:
        logger.warning("⚠️  Modo PRODUCCIÓN — operando con dinero REAL")
        checks.append(("Modo producción", True))

    # Reporte de checks
    logger.info("─" * 50)
    all_passed = True
    for check_name, passed in checks:
        status = "✅" if passed else "❌"
        logger.info(f"  {status} {check_name}")
        if not passed:
            all_passed = False

    logger.info("─" * 50)

    if all_passed:
        logger.info("✅ Todos los checks pasaron — arrancando bot")
    else:
        logger.critical("❌ Checks fallidos — el bot NO arrancará")

    return all_passed


def main():
    # 1. Setup del logger
    setup_logger("INFO")

    # 2. Cargar configuración (falla si faltan vars)
    try:
        settings = load_settings()
        logger.info("✅ Configuración cargada")
    except Exception as e:
        logger.critical(f"❌ Error en configuración: {e}")
        nf.notify_critical(f"❌ Bot no arranca — config: {e}")
        logger.critical("Revisá tu archivo .env. Copiá .env.example como punto de partida.")
        sys.exit(1)

    # 3. Importar módulos (acá para detectar errores de import temprano)
    from core.exchange import ExchangeClient
    from strategies.main_strategy import MainStrategy

    # 4. Startup checks
    try:
        exchange = ExchangeClient(settings)
    except Exception as e:
        logger.critical(f"❌ Fallo iniciando exchange: {e}")
        nf.notify_critical(f"❌ Bot no arranca — exchange: {e}")
        sys.exit(1)

    if not startup_checks(settings, exchange):
        nf.notify_critical("❌ Bot no arranca — startup checks fallaron")
        sys.exit(1)

    # 5. Controller compartido entre listener (thread daemon) y estrategia
    controller = BotController()

    # 6. Iniciar estrategia (registra componentes en el controller)
    try:
        strategy = MainStrategy(settings, controller)
    except Exception as e:
        logger.critical(f"❌ Fallo construyendo strategy: {e}", exc_info=True)
        nf.notify_critical(f"❌ Bot no arranca — strategy: {e}")
        sys.exit(1)

    # 7. Arrancar listener de comandos por Telegram antes del loop principal
    listener = TelegramListener(settings, controller)
    listener.start()

    # Timeframe a segundos
    tf_map = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
    interval = tf_map.get(settings.timeframe, 900)

    try:
        strategy.run_forever(interval_seconds=interval)
    except KeyboardInterrupt:
        logger.info("Bot detenido por usuario (Ctrl+C)")
    except Exception as e:
        # Crash inesperado — notificar a Telegram antes de morir
        logger.critical(f"❌ CRASH inesperado en el loop principal: {e}", exc_info=True)
        nf.notify_critical(
            f"❌ Bot crasheó en el loop principal\n\n<code>{type(e).__name__}: {str(e)[:500]}</code>"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
