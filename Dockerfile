# Demo Cadastro Veicular — container pra Coolify (Napel)
FROM python:3.12-slim

# libmagic1 = backend nativo do python-magic (validação magic bytes de uploads)
# curl = healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependências (cache layer separada pra builds mais rápidas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código fonte
COPY backend.py manifest.json sw.js frontend.html ./
COPY fixtures/ ./fixtures/
COPY seed_filiais.sql ./

# Volume pra dados persistentes (sobrevive a deploys)
VOLUME ["/data"]

# Config padrão pro container
ENV DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8761 \
    USE_REAL_VISION=true \
    VISION_MODEL=claude-haiku-4-5 \
    PYTHONUNBUFFERED=1

EXPOSE 8761

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8761/api/patrimonio/health || exit 1

CMD ["python", "backend.py"]
