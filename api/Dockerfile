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

# jbigkit-bin: jbigkit's JBIG tools (pbmtojbg, jbgtopbm: the full jbg_enc API HylaFAX+ encodes with), which
# measure each fax's pages in JBIG, plain and tuned (hylafax/patches/0003), before
# dialing (api/app/pages/coding.py). jbigkit 2.1 is GPL-2.0-or-later (Markus Kuhn; jbigkit-2.1/COPYING, and
# "(at your option) any later version" in libjbig/jbig85.c). Faxbot only runs them as separate programs, so its
# own code stays under Faxbot's licence (LICENSE). This file installs Debian's binary package; anyone who distributes a built image must
# also offer its source under GPL-2.0 section 3: the matching Debian source package
# (`apt-get source jbigkit` from the same archive), shipped with the image or offered in writing for three years.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ghostscript \
       curl \
       tini \
       jbigkit-bin \
    && rm -rf /var/lib/apt/lists/* \
    && command -v pbmtojbg && command -v jbgtopbm

WORKDIR /app

COPY api/requirements.txt /app/api/requirements.txt
COPY python_mcp/requirements.txt python_mcp/runtime-constraints.txt /app/python_mcp/
RUN pip install --no-cache-dir -r /app/api/requirements.txt -r /app/python_mcp/requirements.txt \
    && python -m pip check

COPY api/app /app/app
COPY api/alembic /app/alembic
COPY api/alembic.ini /app/alembic.ini

# The faxbot command line, for example: docker compose exec api faxbot system health
# Files it makes are its own alone (umask 077), as the server's are.
RUN printf '#!/bin/sh\numask 077\nexec env PYTHONPATH=/app python -m app.cli "$@"\n' > /usr/local/bin/faxbot \
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
# Every file the API makes on the data volume is root's alone unless it says otherwise (umask 077):
# the volume is shared with Asterisk, which reads only the fax images and the inbound secret (0640).
CMD ["sh", "-c", "umask 077 && exec uvicorn app.main:app --host 0.0.0.0 --port 8080"]
