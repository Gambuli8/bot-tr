# 📱 VPS Cheatsheet — agent-trading

Comandos clave para administrar el bot desde el celular vía Termius (o app SSH similar).
Asume que estás en `~/agent-trading` del VPS.

---

## 🟢 Chequeo rápido (10 segundos)

```bash
docker compose -f docker-compose.multi.yml ps && \
for sym in btc sol avax; do
  rc=$(docker inspect --format '{{.RestartCount}}' agent-trading-$sym)
  trades=$(wc -l < data/$sym/trade_journal.jsonl 2>/dev/null || echo 0)
  echo "$sym: Restarts=$rc | Trades=$trades"
done
```

Sano: 4 containers Up healthy, RestartCount=0, Trades creciendo cuando opera.

---

## 📜 Ver logs

```bash
# Vivo de un bot
docker compose -f docker-compose.multi.yml logs -f bot-btc

# Últimas 50 líneas de los 3
for sym in btc sol avax; do
  echo "═══ $sym ═══"
  docker compose -f docker-compose.multi.yml logs --tail=50 bot-$sym
done

# Filtrar errores
docker compose -f docker-compose.multi.yml logs --tail=200 | grep -iE "error|❌|critical|invalid"
```

---

## 💰 Ver capital y trades

```bash
# Capital actual de cada bot (lo que ve el OrderManager)
for sym in btc sol avax; do
  capital=$(cat data/$sym/state.json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('capital','??'))" 2>/dev/null)
  echo "$sym: capital=\$$capital"
done

# Últimos 3 trades de cada bot
for sym in btc sol avax; do
  echo "═══ $sym ═══"
  tail -3 data/$sym/trade_journal.jsonl 2>/dev/null
done
```

---

## ⚙️ Operación

```bash
# Parar todos
docker compose -f docker-compose.multi.yml down

# Levantar todos
docker compose -f docker-compose.multi.yml up -d

# Reiniciar 1 solo
docker compose -f docker-compose.multi.yml restart bot-btc

# Update tras git pull
git pull && \
docker compose -f docker-compose.multi.yml down && \
docker compose -f docker-compose.multi.yml build && \
docker compose -f docker-compose.multi.yml up -d
```

---

## 🚨 EMERGENCIA — kill switch + cerrar manualmente

```bash
# 1. Parar bots ya
docker compose -f docker-compose.multi.yml down

# 2. Cerrar posición abierta desde Binance app móvil:
#    Futures → Positions → "Close Position" con "Market" o "Reduce Only"

# 3. Verificar que no quedó nada
docker compose -f docker-compose.multi.yml ps   # vacío
```

---

## 🔧 Cambiar capital nominal (después de inyección mensual o validación OK)

```bash
docker compose -f docker-compose.multi.yml down

# Cambiar capital en los 3 .env (ajustá el valor viejo y nuevo)
for env in .env.btc .env.sol .env.avax; do
  sed -i 's/^INITIAL_CAPITAL=15$/INITIAL_CAPITAL=70/' $env
done

# Borrar state.json viejo para que tome el INITIAL_CAPITAL nuevo
# (si NO lo borrás, el bot sigue usando el capital viejo del state)
rm -f data/btc/state.json data/sol/state.json data/avax/state.json
rm -f data/btc/controller_state.json data/sol/controller_state.json data/avax/controller_state.json

# Up
docker compose -f docker-compose.multi.yml up -d
```

⚠️ Acordate de **transferir más USDT a Futures Wallet en Binance** antes de subir el capital.

---

## 📊 Verificar conexión a Futures

```bash
for sym in btc sol avax; do
  echo "═══ $sym ═══"
  docker compose -f docker-compose.multi.yml logs --tail=80 bot-$sym | \
    grep -E "Balance|Leverage|Margin mode|Par.*disponible|Precio actual|Invalid|❌"
done
```

Sano = ver por bot:
```
✅ Conexión validada | Balance USDT (Futures wallet): XX.XX
✅ Margin mode = ISOLATED para X/USDT
✅ Leverage = 7× para X/USDT
✅ Par X/USDT disponible (Futures perpetuo)
✅ Precio actual X/USDT: $XX,XXX.XX
```

---

## 🧪 Recursos del VPS

```bash
# Memoria de los bots
docker stats agent-trading-btc agent-trading-sol agent-trading-avax --no-stream

# Disco
df -h
du -sh data/*

# RAM general del VPS
free -h
```

---

## 🐛 Troubleshooting típico

### Bot en restart loop ("Up X seconds" todo el tiempo)

```bash
# Ver por qué muere
docker inspect agent-trading-btc --format 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} Restarts={{.RestartCount}}'

# Si OOM → kernel mata por memoria → subir mem limit en docker-compose.multi.yml
dmesg | grep -iE "killed process|oom" | tail -5

# Si ExitCode != 0 → error en código, ver logs
docker compose -f docker-compose.multi.yml logs --tail=200 bot-btc
```

### "Invalid API-key, IP, or permissions" (error -2015)

Causas (en orden de probabilidad):
1. API key creada antes de activar Futures Wallet → borrar y crear nueva
2. IP del VPS no está en whitelist de la key → `curl ifconfig.me` y agregar
3. Permiso "Enable Futures" no marcado en la key → editar y marcar

### `/status` desde Telegram solo responde 1 bot

Limitación conocida: los 3 bots comparten chat y solo el más rápido contesta.
Para ver los otros, usar los comandos de arriba en el VPS.

### Trades no aparecen tras varios días

Normal con PA en 1h. Frecuencia esperada: ~1 setup cada 5-10 días por bot.
3 bots × esa frecuencia = ~3-4 trades/semana en total.

---

## 📅 Validación 7 días (en curso, hasta 2026-06-16)

### Criterios de aprobación

| # | Criterio | Comando |
|---|---|---|
| 1 | RestartCount=0 en los 3 | Chequeo rápido (arriba) |
| 2 | ≥1 trade por cada bot | `wc -l data/<sym>/trade_journal.jsonl` |
| 3 | Sin warnings reconciliación | `grep -i warning data/*/audit.jsonl` |
| 4 | Memoria estable <500M | `docker stats` |
| 5 | Trades con SL/TP coherentes | Inspeccionar journal |

Si TODO ✅ tras 7 días → subir capital de $15→$70 (sección "Cambiar capital nominal").

---

## 🔗 Referencias

- Estado completo: `docs/HANDOFF.md`
- Auditorías y WFA: `docs/BACKTESTS.md`
- Deploy detallado: `docs/DEPLOYMENT_MULTI_ASSET.md`
