Phase 3 Runbook — Hierarchical Config, Diagnostics, Reliability

Branch: auto-tunnel (all v4 work stays here)
Scope: DB-first hierarchical config, Redis caching + invalidation, canonical events + SSE, webhook hardening + DLQ, provider health + circuit breaker, rate limiting, HIPAA-aligned logging

0) Pre-flight (once per machine)

Checklist

 Git on the right branch and clean working tree

 Python 3.11+ / Node 20+ / Redis 7 / Postgres 14+ reachable

 Uvicorn + Alembic available

 Strong crypto key present (44-char Fernet)

Commands (copy/paste)

echo "== ensure branch auto-tunnel and clean tree" && git fetch --all --tags && git checkout -B auto-tunnel origin/auto-tunnel && git status && \
echo "== versions" && python3 --version && node --version && npm --version && psql --version && redis-server --version && \
echo "== install server deps (uvicorn/alembic/cryptography)" && pip3 install -U pip wheel && pip3 install -r requirements.txt || true && \
echo "== install admin UI deps" && cd api/admin_ui && npm ci && cd - && \
echo "== start local Redis if not present" && (docker ps --format '{{.Names}}' | grep -q '^faxbot-redis$' || docker run -d --name faxbot-redis -p 6379:6379 redis:7-alpine) && \
echo "== generate CONFIG_MASTER_KEY if missing and write .env.local" && python3 - <<'PY'
import os,base64,sys
from pathlib import Path
env = Path(".env.local")
if env.exists(): txt = env.read_text()
else: txt = ""
if "CONFIG_MASTER_KEY=" in txt:
    print("CONFIG_MASTER_KEY already present")
else:
    try:
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
    except Exception:
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()[:44]
    with env.open("a") as f:
        f.write(f"\nCONFIG_MASTER_KEY={key}\n")
    print("CONFIG_MASTER_KEY created")
PY


Stop-check

 git status shows On branch auto-tunnel, no uncommitted changes needed for this run

 .env.local now contains CONFIG_MASTER_KEY= with 44 characters

1) Database migration — hierarchical config + audit + events + DLQ

Checklist

 Migration 003 applied (config tables + audit + events + dlq)

 Alembic heads consistent (no divergence)

Commands

echo "== run Phase 3 migrations" && alembic -c api/db/alembic.ini upgrade head && \
echo "== confirm tables exist" && psql "$DATABASE_URL" -c "\dt+" | egrep -i "config_(global|tenant|department|group|user)|config_audit|events|webhook_dlq"


Stop-check

 Tables config_global/tenant/department/group/user, config_audit, events, webhook_dlq all listed

2) Boot with safe defaults (no PHI, DB-first reads)

Checklist

 Server starts and refuses to boot if CONFIG_MASTER_KEY is invalid

 Admin Console reachable

Commands

echo "== export baseline env (safe defaults)" && \
export FAXBOT_ENV="dev" && export ENABLE_LOCAL_ADMIN="true" && export REDIS_URL="redis://localhost:6379/2" && \
export REQUIRE_API_KEY="true" && export API_KEY="devkey-devkey-devkey" && \
echo "== start server (foreground)" && uvicorn api.app.main:app --host 0.0.0.0 --port 8080


(Open a second terminal for the next steps.)

Stop-check

 http://localhost:8080/health returns 200

 Admin Console loads at http://localhost:8080/admin/

3) Hierarchical Config — effective resolution + safe writes

Checklist

 Effective read works (db/default/cache source indicated)

 Safe key write at tenant and user levels reflects in effective resolution

 Cache invalidation works after write

Commands

echo "== show effective config (admin-capable required; using API key header)" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" "http://localhost:8080/admin/config/effective" | jq '.values|keys' && \
echo "== set tenant override for api.rate_limit_rpm=60" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" -H "Content-Type: application/json" \
  -X POST "http://localhost:8080/admin/config/set" \
  -d '{"key":"api.rate_limit_rpm","value":60,"level":"tenant","level_id":"tenant_demo","reason":"Phase3 tenant default"}' | jq && \
echo "== set user override for api.rate_limit_rpm=5" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" -H "Content-Type: application/json" \
  -X POST "http://localhost:8080/admin/config/set" \
  -d '{"key":"api.rate_limit_rpm","value":5,"level":"user","level_id":"user_demo","reason":"tight user test"}' | jq && \
echo "== query hierarchy for key" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" \
  "http://localhost:8080/admin/config/hierarchy?key=api.rate_limit_rpm" | jq && \
echo "== flush config cache" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" -X POST "http://localhost:8080/admin/config/flush-cache" | jq


Stop-check

 hierarchy.layers[0] shows user value 5 when asking from that user context

 After flush-cache, re-query reflects latest DB values

 No secrets displayed; masked in UI

4) Redis Cache — stats + fallback

Checklist

 Redis stats visible; local cache present

 Service keeps running if Redis is killed (falls back to local)

Commands

echo "== get effective + cache stats" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" "http://localhost:8080/admin/config/effective" | jq '.cache_stats' && \
echo "== simulate Redis outage (container stop)" && docker stop faxbot-redis && sleep 2 && \
echo "== re-query effective (should still work using local cache)" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" "http://localhost:8080/admin/config/effective" | jq '.cache_stats' && \
echo "== restore Redis" && docker start faxbot-redis && sleep 2


Stop-check

 With Redis down, effective config endpoint still works (warning allowed)

 After restart, stats show Redis again

5) Canonical Events + SSE diagnostics (no PHI)

Checklist

 Events table persists entries

 SSE stream shows recent + live events (admin-only)

 Payload metadata contains no PHI

Commands

echo "== tail SSE (keep this running; Ctrl+C to exit later)" && \
curl -N -H "X-API-Key: devkey-devkey-devkey" "http://localhost:8080/admin/diagnostics/events" &
echo "== emit a test event via helper endpoint (if present) or trigger by sending a test fax job" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" "http://localhost:8080/admin/diagnostics/events/recent?limit=5" | jq


Stop-check

 SSE stream prints recent events, then heartbeats; fields are IDs + metadata only

 No names, numbers, or documents appear in payload_meta

6) Provider Health + Circuit Breaker

Checklist

 Health monitor loop running

 Circuit opens after threshold failures and recovers after timeout/success

 Admin Diagnostics reflects status transitions

Commands (force a failure with bogus creds, low threshold)

echo "== lower CB threshold to 1 for provider 'phaxio' (example)" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" -H "Content-Type: application/json" \
  -X POST "http://localhost:8080/admin/config/set" \
  -d '{"key":"provider.phaxio.circuit_breaker_threshold","value":1,"level":"global","reason":"test trip"}' | jq && \
echo "== attempt a send with intentionally bad creds to trip the circuit" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" -F to=+15551234567 -F file=@./example.pdf \
  -X POST "http://localhost:8080/fax" | jq || true && \
echo "== check recent events include PROVIDER_HEALTH_CHANGED" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" "http://localhost:8080/admin/diagnostics/events/recent?limit=20" | jq '.events[]|select(.type=="PROVIDER_HEALTH_CHANGED")'


Stop-check

 A PROVIDER_HEALTH_CHANGED event shows new_status: "circuit_open"

 After timeout or a successful call, status returns to healthy

7) Webhook Hardening + DLQ

Checklist

 Unverified callback is rejected (401/403)

 Bad payload lands in DLQ with allowlisted headers only

 Lookup of job uses (provider_id, external_id)

Commands (simulate bad callback; provider route may vary)

echo "== send bogus callback to provider route to trigger DLQ (adjust path to an enabled provider)" && \
curl -sS -H "Content-Type: application/json" \
  -d '{"fake":"payload","external_id":"does-not-exist"}' \
  "http://localhost:8080/callbacks/phaxio" | jq || true && \
echo "== inspect DLQ entries (via SQL; adjust table/schema if needed)" && \
psql "$DATABASE_URL" -c "SELECT id,provider_id,external_id,status,substr(headers_meta,1,120) AS headers_sample FROM webhook_dlq ORDER BY received_at DESC LIMIT 5;"


Stop-check

 Callback returns 401/403 or handled with DLQ store (no Authorization headers saved)

 DLQ contains sanitized header metadata only

8) Rate Limiting (per key/endpoint)

Checklist

 429 returned with Retry-After when limit exceeded

 Limits are hierarchical (tenant/user can override)

Commands

echo "== set global rate limit for fax send to 1 rpm" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" -H "Content-Type: application/json" \
  -X POST "http://localhost:8080/admin/config/set" \
  -d '{"key":"api.rate_limit_rpm","value":1,"level":"global","reason":"limit test"}' | jq && \
echo "== send two requests quickly; second should 429" && \
BASE="http://localhost:8080" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" -F to=+15551234567 -F file=@./example.pdf -X POST "$BASE/fax" | jq && \
curl -i -sS -H "X-API-Key: devkey-devkey-devkey" -F to=+15551234567 -F file=@./example.pdf -X POST "$BASE/fax" | sed -n '1,20p'


Stop-check

 Second response shows HTTP/1.1 429 and a Retry-After header

9) Admin Console — Configuration Manager & Diagnostics UI

Checklist

 “Configuration Manager” screen renders effective values with source badges

 Safe keys editable; secrets masked

 Diagnostics shows provider states and SSE status

Manual UI actions

Settings → Configuration Manager: verify source chips (db/env/default/cache)

Diagnostics → Events: live SSE updates appear; no PHI

Diagnostics → Providers: status chips reflect health/circuit state

10) Acceptance Sweep (tick all)

Configuration

 User→Group→Department→Tenant→Global→Default resolution verified

 Writes persist without restart; audit entries created

 Safe keys only editable via Admin Console; validation enforced

 Secrets encrypted at rest; masked in UI

Caching

 Redis on; cache hits observed; invalidation works

 Redis off; service continues with local cache

Events & SSE

 Canonical events persisted; SSE streams IDs/metadata only (no PHI)

 Recent events endpoint filters by type/provider

Provider Health

 Health checks run; circuit opens on failures; recovers as configured

 Manual enable/disable reflected in events + UI

Webhooks

 Signature/verification enforced when provider supports it

 Bad callbacks routed to DLQ; headers allowlisted; no secrets persisted

Rate Limiting

 429 with Retry-After for exceeded limits; hierarchical overrides honored

Security

 All admin endpoints require admin_capable trait

 CONFIG_MASTER_KEY length 44; boot fails fast if invalid

 No PHI in logs/streams/events

11) Troubleshooting (one step at a time)

A. Boot fails with crypto errors

echo "== verify CONFIG_MASTER_KEY length is 44 and valid base64" && \
python3 - <<'PY'
import os,base64
k=os.getenv("CONFIG_MASTER_KEY","")
print(len(k),"chars")
base64.urlsafe_b64decode(k.encode())
print("OK: decodable")
PY


If not 44 or decode fails: regenerate key (Section 0) → restart → re-test.

B. Effective config not changing after set()

echo "== flush cache and re-read" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" -X POST "http://localhost:8080/admin/config/flush-cache" | jq && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" "http://localhost:8080/admin/config/hierarchy?key=api.rate_limit_rpm" | jq


If still stale: verify you wrote to the correct level_id; check audit log row exists.

C. SSE seems silent

echo "== fetch recent events directly" && \
curl -sS -H "X-API-Key: devkey-devkey-devkey" "http://localhost:8080/admin/diagnostics/events/recent?limit=10" | jq


If empty: trigger a fax send (success or failure) to generate lifecycle events.

D. Circuit never opens

Lower threshold to 1 (Section 6) and force a failing request. Confirm PROVIDER_HEALTH_CHANGED.

E. 429 not returned

Confirm middleware is enabled and api.rate_limit_rpm not overridden by user/tenant to a higher value.

12) Go/No-Go & Rollback

Go

 All Acceptance Sweep boxes ticked

 Load test: config resolution p95 ≤ 5ms with warm cache

 No PHI observed in any logs/streams

Rollback (quick)

echo "== rollback code to previous tag" && \
git fetch --tags && git checkout <previous_stable_tag> && \
echo "== downgrade DB to pre-003 if necessary" && \
alembic -c api/db/alembic.ini downgrade -1 && \
echo "== restore .env/.env.local backups" && ls -la


(If you need exact step counts for downgrade, run alembic history --verbose first.)

13) Quick Audits (hygiene)
echo "== async ORM anti-patterns" && rg -n "await\s+.*\.merge\(" api/app || true && \
echo "== accidental env fallbacks in provider" && rg -n "os\.getenv\(" api/app/config || true && \
echo "== cache key shapes" && rg -n "cfg:eff" api/app | sort || true && \
echo "== SSE usage sites" && rg -n "Event