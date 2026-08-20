# Sentinel API — FastAPI on Python 3.11
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/apps/api:/app

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY apps/api/pyproject.toml /app/apps/api/pyproject.toml
RUN pip install --upgrade pip && pip install -e /app/apps/api[dev]

COPY apps/api /app/apps/api
COPY scripts /app/scripts

# Never run as root, even in development.
RUN useradd --uid 10001 --create-home sentinel \
 && mkdir -p /run/keys && chown -R sentinel:sentinel /app /run/keys
USER sentinel

WORKDIR /app/apps/api
EXPOSE 8000
CMD ["uvicorn", "sentinel_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
