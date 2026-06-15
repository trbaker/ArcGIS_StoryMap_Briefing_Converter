# Build marker 2026-06-15b — full ArcGIS install (ends the minimal-deps missing-module loop). Forces a fresh commit/redeploy.
# Container build (Render runtime: docker, or a VPS). Full ArcGIS install.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Build tools as insurance for any dependency without a prebuilt wheel.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 0 --max-requests 50 --max-requests-jitter 10"]
