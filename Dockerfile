# Build marker 2026-06-15a — forces a fresh commit so GitHub overwrites the old file and Render redeploys (no functional effect).
# Portability / fallback: identical behavior in a container. Use on Render
# (runtime: docker) or to run the SAME image on a VPS (e.g. a 4 GB Hetzner box)
# if you ever outgrow 512 MB. Uses the MINIMAL arcgis install (no numpy/pandas).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# No build toolchain needed: the minimal dependency set ships as wheels.
# If you later re-add enterprise auth (requests-kerberos / requests-gssapi),
# uncomment the next line to provide the krb5 build libraries they require:
# RUN apt-get update && apt-get install -y --no-install-recommends gcc libkrb5-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 1) Minimal runtime deps (normal install).  2) arcgis with NO transitive deps,
#    so its heavy numpy/pandas/scipy/shapely/pyproj stack is skipped.
RUN pip install -r requirements.txt \
    && pip install --no-deps "arcgis>=2.3,<2.5"

COPY . .

ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 0 --max-requests 50 --max-requests-jitter 10"]
