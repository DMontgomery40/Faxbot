FROM node:24.21.0-alpine AS admin-ui-builder
WORKDIR /build
COPY api/admin_ui/package*.json ./
RUN npm ci
COPY api/admin_ui/ ./
RUN npm run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ghostscript \
       curl \
       tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY api/requirements.txt /app/api/requirements.txt
COPY python_mcp/requirements.txt python_mcp/runtime-constraints.txt /app/python_mcp/
RUN pip install --no-cache-dir -r /app/api/requirements.txt -r /app/python_mcp/requirements.txt \
    && python -m pip check

COPY api/app /app/app
COPY api/alembic /app/alembic
COPY api/alembic.ini /app/alembic.ini

# The faxbot command line, for example: docker compose exec api faxbot system health
RUN printf '#!/bin/sh\nexec env PYTHONPATH=/app python -m app.cli "$@"\n' > /usr/local/bin/faxbot \
    && chmod 755 /usr/local/bin/faxbot

# Copy provider traits and any provider manifests
COPY config /app/config

# MCP (Python) — install and include server for embedded mount
COPY python_mcp /app/python_mcp

# Copy admin UI build from the first stage
COPY --from=admin-ui-builder /build/dist /app/admin_ui/dist


# Data volume mounted at /faxdata by compose

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
