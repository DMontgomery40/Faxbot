# API Tests Overview

Location: `api/tests/`

How to run

Use a development virtual environment with Ghostscript installed. From the repository root:

```sh
cd api
python -m pip install -r requirements.txt -r ../python_mcp/requirements.txt
test_dir=$(mktemp -d)
FAX_DATA_DIR="$test_dir/artifacts" DATABASE_URL="sqlite:///$test_dir/faxbot.db" \
  FAX_DISABLED=true python -m pytest -q
```

Use isolated test storage and synthetic credentials; never point the suite at an operator installation. The production container does not bundle this test suite.

Test files (high level)
- `test_api.py` — health and basic `/fax` validation
- `test_api_keys.py` — admin key mint/list/revoke, send and read with token
- `test_api_scopes.py` — scope enforcement for send/read
- `test_rate_limit.py` — per‑key rate limiting
- `test_phaxio.py` — Phaxio send path (mocked), status mapping, callback, PDF token endpoint
- `test_inbound_internal.py` — internal inbound post/list/get/PDF (scoped)
- `test_freeswitch.py` — held FreeSWITCH jobs refuse results without a verified issued attempt, preserving the job and its history
- `test_outbound_store.py` and `test_outbound_worker.py` — durable acceptance, dispatch ownership, held work, and ambiguous submission recovery
- `test_outbound_polling.py` and `test_outbound_callbacks.py` — status observations use each fax's captured provider account and attempt

Tips
- `FAX_DISABLED=true` accepts new uploads as held test jobs. They have no issued provider attempt and never transmit automatically after sending is enabled. A held job cannot be marked delivered by a fabricated result callback.
- Unit and integration checks supplement user-facing acceptance. Verify operator workflows through the actual browser or desktop UI, including visible state after restart and provider uncertainty.
- Use a temporary `FAX_DATA_DIR` per test run to isolate artifacts.
