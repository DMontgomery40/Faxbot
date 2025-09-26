#!/usr/bin/env bash
set -euo pipefail

echo "[build]"
docker compose build

echo "[up]"
docker compose up -d

echo "[health check (best effort)]"
curl -fsS http://localhost:8080/health || true

echo "[/metrics exists]"
curl -fsS http://localhost:8080/metrics | head -n 3 >/dev/null

echo "[providers endpoint shape]"
curl -fsS -H "X-API-Key: ${API_KEY:-dev}" http://localhost:8080/admin/providers | jq -e 'type=="object"' >/dev/null

echo "[callbacks return 202 (idempotent no-op is fine)]"
code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST http://localhost:8080/phaxio-callback \
  -H "Content-Type: application/json" -H "X-Phaxio-Signature: test" -d '{"id":"smoke-pr","status":"success"}')
test "$code" -eq 202

code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST http://localhost:8080/sinch-inbound \
  -H "Authorization: Basic dGVzdDp0ZXN0" -H "Content-Type: application/json" -d '{"id":"smoke-pr"}')
test "$code" -eq 202

echo "[openapi diff + traits schema are separate CI jobs]"
echo "OK"
