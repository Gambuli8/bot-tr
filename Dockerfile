FROM python:3.12-slim

# Mensajes en tiempo real (sin buffer) y locale UTF-8 para emojis
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    LANG=C.UTF-8

# Dependencias del sistema: tzdata para zona horaria correcta en logs.
# Ya no necesitamos build-essential (sacamos pandas-ta).
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer separado para deps (mejor caching de Docker)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copiamos el código del proyecto
COPY . .

# Carpeta runtime persistente (state, journal, audit, bot.log)
RUN mkdir -p /app/data

# Healthcheck: el bot está vivo si el heartbeat (data/heartbeat) se actualizó en
# los últimos 5 min. health.beat() lo reescribe al final de cada ciclo del loop;
# si el loop se cuelga, el archivo queda viejo y el container pasa a "unhealthy".
# (Más explícito que mirar el mtime del log: es una señal de vida dedicada.)
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD test $(($(date +%s) - $(cat /app/data/heartbeat 2>/dev/null || echo 0))) -lt 300 || exit 1

# El bot por sí solo no expone puertos (no HTTP, sólo Telegram polling).
# Si en el futuro agregás un /metrics, exponé acá.

CMD ["python", "-u", "main.py"]
