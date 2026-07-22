# Damanat PMS — Python AI Backend (Production)
# =========================
# Stage 1: Builder
# =========================
FROM python:3.11-slim as builder

WORKDIR /app

ENV ACCEPT_EULA=Y \
    DEBIAN_FRONTEND=noninteractive

# Install build + ODBC deps
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        gcc \
        curl \
        gnupg \
        unixodbc \
        unixodbc-dev; \
    \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg; \
    \
    curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        -o /etc/apt/sources.list.d/mssql-release.list; \
    \
    apt-get update; \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18; \
    \
    ldconfig; \
    \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# =========================
# Stage 2: Runtime
# =========================
FROM python:3.11-slim

WORKDIR /app

ENV ACCEPT_EULA=Y \
    DEBIAN_FRONTEND=noninteractive

# Install runtime ODBC libs (must be present in runtime stage, not only in builder)
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        unixodbc; \
    \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg; \
    \
    curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        -o /etc/apt/sources.list.d/mssql-release.list; \
    \
    apt-get update; \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18; \
    \
    ldconfig; \
    \
    rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /root/.local /root/.local

# Copy app
COPY . .

# Env
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GUNICORN_TIMEOUT_SECONDS=90 \
    PORT=8080

# Logs
RUN mkdir -p logs && chmod 755 logs

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD curl -f http://localhost:${PORT}/api/v1/health || exit 1

# =========================
# Entrypoint
# =========================
# Single worker is required: ANPR ↔ CAM-03 entry-confirmation handshake in
# app/services/entry_exit_service.py stores transient state in module-level
# dicts (_pending_entries, _cam03_pre_confirmations). With multiple workers
# the ANPR event and the CAM-03 event can land in different processes that
# don't share that state, causing real entries to be dropped as "ghosts".
# Same applies to the occupancy dedup cache and event_bus subscriber list.
RUN cat > /app/entrypoint.sh << 'EOF'
#!/bin/bash
set -e

echo "[$(date)] ✨ Starting Damanat PMS AI Backend..."

# Optional: migrations
# echo "[$(date)] 🔄 Running database migrations..."
# alembic upgrade head

echo "[$(date)] 🚀 Starting Gunicorn server..."

exec gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w 1 \
  --timeout "${GUNICORN_TIMEOUT_SECONDS}" \
  --bind 0.0.0.0:${PORT} \
  --access-logfile - \
  --error-logfile - \
  --log-level info
EOF

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
