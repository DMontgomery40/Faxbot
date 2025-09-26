# Prebuilt base (optional): FROM ghcr.io/${OWNER}/faxbot-base:py3.11-node18-gs as base
# Fallback lightweight base when prebuilt base is not yet available
FROM python:3.11-slim as base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ghostscript \
       curl \
       tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) Dependencies layer (cache on requirements.txt)
COPY api/requirements.txt /app/requirements.txt
RUN python -m venv /.venv \
 && . /.venv/bin/activate \
 && pip install --no-cache-dir --upgrade pip wheel \
 && pip install --no-cache-dir -r /app/requirements.txt

# 2) App code
COPY api/app /app/app
COPY config /app/config

# MCP (Python) — optional
COPY python_mcp/requirements.txt /app/python_mcp/requirements.txt
RUN . /.venv/bin/activate \
 && pip install --no-cache-dir -r /app/python_mcp/requirements.txt || true
COPY python_mcp /app/python_mcp

ENV PATH="/.venv/bin:${PATH}"
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]


