FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY backend-cluster/ledger-python/requirements.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt

COPY cli/src/fava ./fava
COPY backend-cluster/ledger-python/vendor/fava-init.py ./fava/__init__.py
COPY cli/src/bea_engine ./bea_engine
COPY backend-cluster/ledger-python/app ./app

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
