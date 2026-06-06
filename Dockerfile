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

# Carpetas runtime (state, journal, audit, logs rotados)
RUN mkdir -p /app/data /app/logs

# Healthcheck: el bot está vivo si el log se actualizó en los últimos 3 min.
# Útil para `docker ps` y para detectar bots colgados (ej. red caída).
HEALTHCHECK --interval=2m --timeout=10s --start-period=60s --retries=2 \
    CMD test $(($(date +%s) - $(stat -c %Y /app/logs/bot.log 2>/dev/null || echo 0))) -lt 180 || exit 1

# El bot por sí solo no expone puertos (no HTTP, sólo Telegram polling).
# Si en el futuro agregás un /metrics, exponé acá.

CMD ["python", "-u", "main.py"]
