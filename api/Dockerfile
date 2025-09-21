FROM node:18-alpine AS admin-ui-builder
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

COPY api/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY api/app /app/app

# Copy provider traits and any provider manifests
COPY config /app/config

# MCP (Python) — install and include server for embedded mount
COPY python_mcp/requirements.txt /app/python_mcp/requirements.txt
RUN pip install --no-cache-dir -r /app/python_mcp/requirements.txt
COPY python_mcp /app/python_mcp

# Copy admin UI build from the first stage
COPY --from=admin-ui-builder /build/dist /app/admin_ui/dist

# Copy assets for the admin UI
COPY assets /app/assets

# Data volume mounted at /faxdata by compose

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
