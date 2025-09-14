# Deployment Guide (Production‑Ready)

This guide summarizes recommended production architecture and configuration so today’s changes remain compatible with future hosting (containers, managed DB, S3, etc.). It keeps Faxbot open‑core friendly and avoids rework later.

Key principles
- Containerize the API; run behind a reverse proxy with TLS (Nginx/Caddy/ALB/Traefik).
- Use a managed relational database (PostgreSQL recommended). SQLite is for dev only.
- Store artifacts (PDF/TIFF) in S3/S3‑compatible storage with SSE‑KMS (BAA for HIPAA). Local disk for dev only.
- Keep Asterisk isolated on internal networks if using SIP; never expose AMI.
- Frontend (e.g., Netlify) stays separate and calls the API via HTTPS.

Components
- API (FastAPI): containerized, stateless; scale horizontally behind LB.
- Database: PostgreSQL with proper backups, TLS, and minimal privileges.
- Object storage: S3/S3‑compatible with SSE‑KMS for artifacts; lifecycle policies.
- Reverse proxy/WAF: TLS, rate limiting, IP restrictions, OIDC if needed.
 - Health checks: use `GET /health` for liveness and `GET /health/ready` for readiness (returns 200 when core dependencies are ready; 503 otherwise).
- Optional: Asterisk (only for SIP flows), isolated in a VPC/VNet; UDPTL ports open to trunk provider only.

Environment configuration
- Database
  - `DATABASE_URL=postgresql+psycopg2://USER:PASS@HOST:5432/DBNAME`
  - Pooling: SQLAlchemy provides connection pooling; add PgBouncer if needed.
  - Migrations: Prefer Alembic migrations for schema changes. Fresh installs use `Base.metadata.create_all`. Existing SQLite-only ad‑hoc migrations are best-effort.
- Authentication
  - `REQUIRE_API_KEY=true` in production. Create per‑user/service keys with `/admin/api-keys`.
  - MCP SSE requires OAuth2/JWT; host under TLS.
- Storage
  - `STORAGE_BACKEND=s3`
  - `S3_BUCKET`, `S3_PREFIX`, `S3_REGION`
  - `S3_ENDPOINT_URL` for MinIO (on‑prem); `S3_KMS_KEY_ID` for SSE‑KMS.
- Inbound (optional)
  - `INBOUND_ENABLED=true`
  - SIP/Asterisk: `ASTERISK_INBOUND_SECRET=<random>` (keep internal; rotate regularly)
  - Phaxio: `PHAXIO_INBOUND_VERIFY_SIGNATURE=true` (HMAC with `PHAXIO_API_SECRET`)
  - Sinch: `SINCH_INBOUND_BASIC_USER/PASS`, `SINCH_INBOUND_HMAC_SECRET`
- Rate limits (defense in depth)
  - App level: `MAX_REQUESTS_PER_MINUTE`, `INBOUND_LIST_RPM`, `INBOUND_GET_RPM`
  - Edge/WAF/proxy: enforce additional limits and IP allowlists.

Stateful vs stateless considerations
- In‑memory features (rate limiting buckets) are node‑local. For multiple API instances, keep edge WAF limits primary; later we can add Redis for distributed limits.
- AMI (Asterisk) listeners should run on a single instance or a dedicated worker to avoid duplicate consumption.
- Inbound idempotency uses DB unique index and is safe across instances.

Frontend hosting (e.g., Netlify)
- Host only the UI on Netlify; point it to the API base URL over HTTPS.
- Do not host the Faxbot API as Netlify functions; FastAPI + artifact handling + Ghostscript + SIP/Asterisk are not serverless‑friendly.

HIPAA notes
- Execute BAAs with providers (Phaxio/Sinch, cloud infra, S3/KMS).
- Enforce HTTPS, HMAC verification, short token TTLs, audit logging.
- Store no PHI in logs; artifact storage encrypted at rest.

Next steps for production maturity
- Alembic migrations: add migrations for all tables and future schema changes.
- Distributed rate limiting: optional Redis‑backed limiter for multi‑instance.
- Observability: ship structured logs to your SIEM, metrics via OTEL/Prometheus.


## BAA and Subprocessor Matrix

When you operate Faxbot as a hosted service that processes, transmits, or stores PHI for customers, you are a Business Associate. You must sign BAAs with customers and maintain BAAs (or appropriate HIPAA agreements) with all subprocessors that can access PHI.

Recommended approach
- Maintain a living subprocessor inventory and data‑flow diagram.
- Execute BAAs with each PHI‑touching vendor before onboarding a healthcare customer.
- Document incident response and breach notification timelines to match BAA commitments.

Subprocessor matrix (typical small‑hospital deployment)

| Category | Example vendors | PHI exposure | BAA required | Notes |
|---|---|---|---|---|
| Cloud IaaS/PaaS (compute/LB) | AWS/GCP/Azure | Possible (network, disks, backups) | Yes | Run API behind TLS; restrict access; use HIPAA‑eligible services only. |
| Object storage | S3 or S3‑compatible (KMS) | Yes (artifact PDFs/TIFFs) | Yes | Enforce SSE‑KMS, bucket policies, lifecycle rules. |
| Relational database | Managed Postgres | Possible (metadata) | Yes | Encrypt at rest, TLS in transit, backups with PITR. |
| Fax transport provider | Phaxio/Sinch | Yes (fax content, numbers) | Yes | Enable webhook signature verification; disable provider storage if policy requires. |
| SIP trunk provider (self‑hosted path) | Bandwidth/Twilio/etc. | Yes (fax media) | Yes | Limit ports/IPs; T.38; keep AMI internal; no public exposure. |
| Logging/monitoring | SIEM/OTEL backend | Avoid storing PHI | Prefer BAA | Log IDs/metadata only; scrub numbers/content. |
| Email/helpdesk | Support desk | Avoid PHI | Prefer BAA or forbid PHI | In contracts, forbid sending PHI to support unless covered by BAA. |
| CDN/static site | Marketing/docs hosting | No PHI | No (for non‑PHI only) | Do not serve PHI content via public CDN; disable analytics for HIPAA sites. |

Customer hosting vs. self‑hosting
- If customers self‑host Faxbot and you do not access PHI, a BAA with the customer is generally not required. Avoid support practices that expose PHI unless under a BAA.
- For faxbot.net (hosted service), you must have BAAs with customers and all PHI‑touching subprocessors.


## Security Headers (production)

Apply at the edge (reverse proxy/WAF) and ensure application responses for PHI endpoints include strict cache controls.

Required headers (HIPAA‑friendly defaults)
- Strict‑Transport‑Security: `max-age=31536000; includeSubDomains; preload`
- Content‑Security‑Policy: allow only required origins; no inline scripts; example below
- X‑Content‑Type‑Options: `nosniff`
- Referrer‑Policy: `no-referrer`
- X‑Frame‑Options: `DENY` (or `SAMEORIGIN` if embedding is required)
- Permissions‑Policy: explicitly disable unneeded features (camera, geolocation, microphone)
- Cache‑Control (PHI endpoints): `no-store, no-cache, must-revalidate`
- Pragma: `no-cache`
- Expires: `0`
- CORS: restrict `Access-Control-Allow-Origin` to your UI origin; do not use `*` when credentials or PHI are involved

Example Nginx snippet
```
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy no-referrer always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
# For API routes that may return PHI
location / {
  add_header Cache-Control "no-store, no-cache, must-revalidate" always;
  add_header Pragma "no-cache" always;
  add_header Expires 0 always;
}
```

Example CSP (tighten domains to only what you use)
```
Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self' https://api.yourdomain.com; base-uri 'none'; form-action 'self';
```

Cookie guidance (if your UI uses sessions)
- Mark cookies Secure, HttpOnly, SameSite=Strict.
- Never include PHI in URLs, query strings, or client‑side storage.

Operational checks
- Enforce TLS everywhere; redirect HTTP→HTTPS.
- Validate webhooks with HMAC/signatures; IP allowlist if available.
- Ensure no PHI in logs; log IDs and generic metadata only.
