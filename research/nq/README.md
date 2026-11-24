# NQ + Apex: ¿hay una estrategia que valga la prueba de US$ 119?

Objetivo: antes de pagar la cuenta **Apex 50K EOD (Tradovate, sin fee de activación)**, medir con datos
reales de NQ qué estrategia de **1 operación por día** tiene ventaja y qué riesgo por operación maximiza la
chance de cobrar el primer retiro.

| Script | Qué hace |
|---|---|
| `download_nq.py` | Baja velas de 1 min de NQ desde Databento (te muestra el costo y pide confirmación) |
| `nq_backtest.py` | Prueba ORB, ORB-2R, ORB-ATR, momentum, la estrategia de Alex (2 versiones) y un control al azar |
| `apex_sim.py` | Corre las operaciones reales de una estrategia contra las reglas de tu cuenta Apex |

## Paso a paso en tu PC (Windows)

Abrí una terminal en la carpeta del proyecto:

```bat
cd C:\Users\aleja\Desktop\agent-trading
git fetch origin claude/amazing-cori-4fr2od
git checkout claude/amazing-cori-4fr2od
venv\Scripts\activate
pip install -r research\nq\requirements.txt
```

### 1. API key de Databento
1. Creá la cuenta en databento.com (te dan crédito gratis).
2. Copiá la API key (empieza con `db-`).
3. Abrí el archivo `.env` de la raíz del proyecto y agregá una línea:
   ```
   DATABENTO_API_KEY=db-tu-key
   ```
   El `.env` no se sube a GitHub. **No pegues la key en ningún chat.**

### 2. Bajar los datos (una sola vez)
```bat
python research\nq\download_nq.py
```
Te muestra el costo por año y el total **antes** de gastar. Respondé `s` para bajar.
Quedan en `data\nq\` (unos cientos de MB). Si se corta, volvé a correrlo: sigue desde donde quedó.

### 3. Backtest de las estrategias
```bat
python research\nq\nq_backtest.py
```
Tarda unos minutos (la de Alex es la más lenta). Imprime, para cada estrategia, el período para elegir
(IS, hasta 2022) y el de validación (OOS, desde 2023), y cuántas corridas al azar le gana.

**Una estrategia sirve sólo si:** es positiva en IS **y** en OOS, con t ≥ 2, y le gana a casi todas las
corridas al azar. Si ninguna cumple, la conclusión es "no comprar la prueba todavía".

### 4. Simulador de Apex con la mejor
```bat
python research\nq\apex_sim.py --strategy ORB
```
Cambiá `ORB` por la que haya salido mejor. Muestra, para riesgos de US$ 250, 375 y 500 en la prueba
(y US$ 250 en la PA): cuántas pruebas pasan, cuántas se queman o vencen, cuántas PA llegan al primer
retiro y el valor esperado de cada prueba de US$ 119.

### 5. Pasame los resultados
Copiá **todo** lo que imprimen los pasos 3 y 4 y pegalo en el chat. Con eso decidimos estrategia y riesgo.

## Opciones útiles
```bat
python research\nq\nq_backtest.py --only ORB,ORB-ATR,RANDOM          :: sólo algunas
python research\nq\nq_backtest.py --risk 375 --commission 1.5        :: riesgo y comisión por micro
python research\nq\apex_sim.py --strategy ORB --eval-risks 300,375,450 --period all
```

## Supuestos a confirmar con Apex
- Drawdown EOD de US$ 2.000, pérdida diaria máxima de US$ 1.000, objetivo US$ 3.000, vence a los 30 días.
- PA: el drawdown se congela en US$ 50.100; retiro con saldo ≥ 52.600, 5 días con ≥ US$ 250 y consistencia
  del 50 %; primer retiro máximo US$ 1.500.
- Límite de micros al inicio de la PA ("scaling"): el simulador usa 30 (`--pa-max-micros`).
- Comisión por micro en Tradovate/Apex: el backtest usa US$ 1,50 ida y vuelta (`--commission`).
- **Bots prohibidos en todas las cuentas Apex**: esto es para elegir qué operar a mano, no para automatizar.
