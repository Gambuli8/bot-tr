# Bot de Trading BTC/USDT
> Python · Binance Testnet · Claude API · Telegram

---

## Setup en 5 pasos

### 1. Clonar y crear entorno virtual
```bash
git clone <tu-repo>
cd trading-bot
python -m venv venv
source venv/bin/activate      # Linux/Mac
# venv\Scripts\activate       # Windows
```

### 2. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 3. Configurar variables de entorno
```bash
cp .env.example .env
# Editá .env con tus API keys
```

**Keys necesarias:**
- **Binance Testnet:** https://testnet.binance.vision → Register → API Management
- **Anthropic:** https://console.anthropic.com → API Keys
- **Telegram:** Hablar con @BotFather → /newbot → obtener token. Luego hablar con @userinfobot para obtener tu chat_id

### 4. Verificar configuración
```bash
python -c "from config.settings import load_settings; s = load_settings(); print('Config OK')"
```

### 5. Arrancar el bot
```bash
python main.py
```

---

## Estructura del proyecto
```
bot/
├── config/
│   ├── settings.py       # Variables globales y validación
│   └── .env              # API keys (no en git)
├── core/
│   ├── exchange.py       # Conexión Binance via ccxt
│   ├── indicators.py     # RSI, MACD, EMA, ATR, Bollinger
│   └── claude_agent.py   # Agente de decisión con Claude
├── strategies/
│   └── main_strategy.py  # Orquestador del ciclo de trading
├── execution/
│   └── order_manager.py  # Órdenes, stop-loss, capital
├── notifications/
│   └── telegram.py       # Alertas en tiempo real
├── logs/
│   └── logger.py         # Sistema de logs con loguru
├── data/                 # Estado y journal (generado en runtime)
├── backtest/             # Motor de backtesting (Fase 7)
├── tests/                # Tests unitarios
├── requirements.txt
├── .env.example
└── main.py               # Entry point
```

---

## Indicadores utilizados
| Indicador | Parámetros | Uso |
|-----------|-----------|-----|
| RSI | 14 | Entrada (< 35) / Salida (> 65) |
| MACD | 12,26,9 | Confirmación de cruce alcista |
| EMA50 | 50 | Tendencia de corto plazo |
| EMA200 | 200 | Filtro de tendencia dominante |
| Bollinger Bands | 20,2 | Volatilidad y extremos de precio |
| ATR | 14 | Cálculo dinámico de stop-loss |

---

## Gestión de riesgo
- Máximo **2%** del capital por operación
- **30%** del capital en reserva (nunca se toca)
- Stop-loss dinámico basado en ATR × 1.5
- Ratio mínimo riesgo/recompensa: **1:2**
- Límite de drawdown diario: **10%** → bot se detiene automáticamente
- Circuit breaker de Claude: 3 fallos consecutivos → modo SAFE

---

## Criterios para pasar a dinero real (Fase 9)
Después de mínimo **4 semanas** de paper trading:
- ✅ Win rate > 50%
- ✅ Profit Factor > 1.3
- ✅ Max Drawdown < 12%
- ✅ Cero errores críticos sin manejar
- ✅ Logs limpios y completos

---

## Comandos útiles
```bash
# Ver logs en tiempo real
tail -f logs/bot_$(date +%Y-%m-%d).log

# Estadísticas actuales
python -c "
from config.settings import load_settings
from execution.order_manager import OrderManager
s = load_settings()
om = OrderManager(s)
import json; print(json.dumps(om.get_stats(), indent=2))
"
```
