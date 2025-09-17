---
layout: default
title: API Tests Overview
parent: Scripts and Tests
nav_order: 4
permalink: /scripts-and-tests/api-tests.html
---

# API Tests Overview

Location: `api/tests/`

Quick map of core tests so you know what’s covered and how to run them.

How to run
- In Docker: `make test`
- Locally (no Docker): create a venv and `pytest api/tests` (the helper `scripts/smoke-auth.sh` demonstrates a minimal path).

Test files
- `test_api.py`
  - Health endpoint and basic `/fax` validation (TXT send path, number validation).
- `test_api_keys.py`
  - Admin key mint/list/revoke; using minted token to send a fax and read status; revoked key rejection.
- `test_api_scopes.py`
  - Scope enforcement for `fax:send` vs `fax:read` on `/fax` and `/fax/{id}`.
- `test_rate_limit.py`
  - Per‑key rate limiting (e.g., MAX_REQUESTS_PER_MINUTE).
- `test_phaxio.py`
  - Phaxio service initialization, send flow (mocked), status mapping, callback handling, and PDF token endpoint behavior.
- `test_inbound_internal.py`
  - Internal Asterisk inbound post → list → get → PDF download guarded by scopes.
- `test_freeswitch.py`
  - FreeSWITCH disabled‑mode send path with simulated outbound result callback → job updated to SUCCESS.

Fixtures
- `conftest.py`
  - Shared fixtures and test settings.

Tips
- Set `FAX_DISABLED=true` to simulate successful sends without contacting a provider.
- Use a temporary `FAX_DATA_DIR` per test run to keep artifacts isolated.

