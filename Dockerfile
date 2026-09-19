FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    LANG=C.UTF-8

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot/ bot/
RUN mkdir -p /app/data

EXPOSE 8080
# Sano = el servidor responde y el monitor late (heartbeat reciente).
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/tv/health', timeout=5).status == 200 else 1)"

# Un solo worker: el estado (posiciones, señales vistas) vive en este proceso.
CMD ["uvicorn", "bot.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
