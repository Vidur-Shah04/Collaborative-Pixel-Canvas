FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY shard ./shard
COPY shared ./shared

CMD ["sh", "-c", "python -m uvicorn shard.main:app --host 0.0.0.0 --port \"${PORT:-8001}\""]
