# Paper bot BTC/USDT 4h. Build desde la raíz del repo:
#   docker build -f quant/Dockerfile -t quant-paper .
#   docker run -d --name quant-paper --restart unless-stopped -v $PWD/data/quant:/app/data/quant quant-paper
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 TZ=UTC \
    QUANT_DATA_DIR=/app/data/quant BINANCE_DATA_HOST=api.binance.com
WORKDIR /app
COPY requirements-quant.txt .
RUN pip install -r requirements-quant.txt
COPY quant/ quant/
# Por defecto opera sólo una estrategia validada (passed=true). Para la prueba forward de la candidata:
#   docker run ... quant-paper python -m quant.bot --candidate
# Sale con 1 tras ~30 fallos seguidos (Docker lo reinicia) y con 2 si salta el halt por drawdown.
CMD ["python", "-m", "quant.bot"]
