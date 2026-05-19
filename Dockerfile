# polymarket_alpha — continuous ingest workers + daily backup
# Runs identically under Docker and OrbStack (Linux containers on macOS).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POLYMARKET_ALPHA_DB=/data/data.db

# rclone is only needed by the backup service; small and harmless in one image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends rclone ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY polymarket_alpha ./polymarket_alpha
COPY scripts ./scripts
# Editable install guarantees the packaged migrations/*.sql resolve at runtime.
RUN pip install -e . \
    && useradd -m app \
    && mkdir -p /data /backups \
    && chmod +x scripts/*.sh \
    && chown -R app /data /backups /app

USER app
VOLUME ["/data", "/backups"]

# Default: run all five workers continuously (concurrent, self-healing).
ENTRYPOINT ["python", "-m", "polymarket_alpha"]
CMD ["worker", "all"]
