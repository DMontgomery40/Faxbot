Admin Console–Only Operations: Execution Plan for a Junior Developer
Below is a complete, explicit, step-by-step prompt to implement an “Admin Console only” operational experience. You won’t write new code here; you’ll follow these instructions to add endpoints, UI wiring, and docs so operators can do everything in the browser after a single docker compose up -d. Follow this in order. Do not mix backends. Do not rename or break existing endpoints.
What you’re building
A Docker-first Faxbot where operators do everything from the local Admin Console UI after a single docker compose up -d.
No terminal required after initial compose. Admin Console handles backend selection (Phaxio/Sinch/SIP), auth keys, diagnostics, send tests, inbound viewing, storage config, and basic MCP connectivity checks.
UI supports smoke tests that mirror scripts in faxbot/scripts/.
Read this first
Name is “Faxbot” (and “faxbot-mcp” for the MCP server). Don’t invent other names.
Backends are mutually exclusive: phaxio | sinch | sip. Gate all copy and UI by active backend. Never show cross-backend options.
MCP: two servers (Node and Python) each with stdio / HTTP / SSE options. Admin Console is not an MCP client; only show config and liveness checks.
HIPAA defaults for healthcare; provide opt-in relaxed paths for non‑PHI development and label them clearly.
File types: PDF and TXT only. REST file size limit 10 MB. Node MCP HTTP/SSE JSON payloads effectively ~16 MB with base64 overhead; prefer MCP stdio for large files.
Test mode sets FAX_DISABLED=true (no real sends). Support in UI and tests.
Base your changes on the development branch and the actual code in this repo. Do not guess.
Success criteria
After docker compose up -d, an operator can:
Open http://localhost:8080/admin/ui/
Log in with a bootstrap admin key from .env (or a DB key with keys:manage)
Choose backend in Setup Wizard, validate creds, Apply & Reload, export .env
Create user API keys with scopes, send a test fax, list jobs, see diagnostics
Enable inbound (per backend), list/download inbound, configure storage (local/S3)
Run “Tests” from Admin Console: environment checks, send flow sim, inbound sim, Phaxio/Sinch callback sims, MCP connectivity checks
Optionally click “Restart API” in Diagnostics when ADMIN_ALLOW_RESTART=true
Baseline: current state (from code)
API (FastAPI) exposes admin endpoints and mounts the Admin UI when ENABLE_LOCAL_ADMIN=true.
Admin endpoints: /admin/config, /admin/settings{,/validate,/export,/reload}, /admin/restart, /admin/health-status, /admin/fax-jobs{,/id}, /admin/api-keys{,/id, /id/rotate}, /admin/diagnostics/run
Core REST: /fax, /fax/{id}, /fax/{id}/pdf (tokenized), /health, /health/ready
Inbound (with INBOUND_ENABLED=true): /_internal/asterisk/inbound, /phaxio-inbound, /sinch-inbound, GET /inbound{,/id,/id/pdf}
Admin UI (Vite/React/MUI) contains Dashboard, Setup Wizard, Jobs, Keys, Settings, Diagnostics, Inbound, Send.
Docker: api/Dockerfile builds Admin UI and assets into the API image; docker-compose.yml maps 8080:8080 and sets ENABLE_LOCAL_ADMIN=true.
Node MCP (HTTP 3001, SSE 3002) and Python MCP (SSE 3003) services exist behind compose profiles: mcp.
Shell scripts under scripts/ provide smoke tests; we’ll replicate their logic in Python and expose via /admin/tests/*.
Roadmap overview (phases)
1) Docker-first boot and admin authentication
2) Setup Wizard end-to-end apply and .env export
3) Settings coverage and gaps
4) Diagnostics → Tests endpoints and UI
5) Inbound inbox polish
6) Send flow safety
7) MCP diagnostics page
8) Documentation updates
9) QA and acceptance matrix
Phase 0: Prep and guardrails
Read:
AGENTS.md (root)
docs-site/docs/admin-console/index.md, PHAXIO_SETUP.md, SINCH_SETUP.md, SIP_SETUP.md, MCP_INTEGRATION.md, LOCAL_ADMIN_CONSOLE.md, SCRIPTS_TESTS_INDEX.md
api/app/main.py, api/app/config.py, api/admin_ui/src/components/*, docker-compose.yml
Rules:
No breaking changes. Do not rename existing routes. New endpoints go under /admin/tests/*.
Backend isolation in UI. Never leak cross-backend strings or fields.
Secrets are masked on GET; UI must never echo back full secrets.
Return structured JSON from tests; avoid 500s; handle errors gracefully.
Deliverable: one-paragraph summary (for PR) describing what Admin Console already does and what you will add.
Phase 1: Docker-first boot
Goal: Operator boots with docker compose up -d and logs into Admin UI.
Steps:
Confirm Admin UI is built into API image (api/Dockerfile copies admin_ui/dist).
Ensure docker-compose.yml sets ENABLE_LOCAL_ADMIN=true on api.
Ensure .env.example documents API_KEY required for Admin Console login; include an example value.
Admin restart:
Confirm api/app/config.py has admin_allow_restart. It exists. No code change now.
Add copy in docs that ADMIN_ALLOW_RESTART=true enables a Restart button.
Acceptance check:
With .env containing API_KEY, run docker compose up -d.
Visit http://localhost:8080/admin/ui/ and login with API_KEY. All tabs render; Diagnostics loads; Settings loads.
Phase 2: Setup Wizard — End-to-end apply
Goal: Wizard lets users choose backend, validate credentials, apply live, and export .env snippet.
Requirements by backend:
Phaxio:
Required: PHAXIO_API_KEY, PHAXIO_API_SECRET, PUBLIC_API_URL, PHAXIO_STATUS_CALLBACK_URL, PHAXIO_VERIFY_SIGNATURE=true
Enforce HTTPS for PUBLIC_API_URL in production if ENFORCE_PUBLIC_HTTPS=true
Sinch:
Required: SINCH_PROJECT_ID, SINCH_API_KEY, SINCH_API_SECRET
SIP:
Required: AMI host/port/username/password, Station ID; password must not be changeme
Steps:
Review POST /admin/settings/validate and PUT /admin/settings in api/app/main.py.
Phaxio validation calls https://api.phaxio.com/v2.1/account/status
SIP validation calls test_ami_connection
Sinch validation checks presence only (v1)
Update Wizard copy:
Phaxio: show reminder to set PUBLIC_API_URL and display derived PHAXIO_STATUS_CALLBACK_URL
SIP: red warning: “AMI must NEVER be exposed to the internet. Port 5038 must remain internal.”
Keep “Apply & Reload”: PUT /admin/settings then POST /admin/settings/reload, then re-run POST /admin/settings/validate and show checks inline.
Generate .env snippet:
Include ENABLE_LOCAL_ADMIN=true
Phaxio path writes PHAXIO_VERIFY_SIGNATURE=true
Sinch path includes project ID and keys
SIP path writes ASTERISK_AMI_PASSWORD exactly; warn if value equals changeme
Persistence copy:
“Applied in-process; for persistence across restarts export .env then restart.”
Deliverables:
Copy tweaks only. No new endpoints.
Test Wizard for all 3 backends end-to-end.
Phase 3: Settings coverage — fill gaps
Goal: Settings tab controls all relevant toggles.
Add/verify in Settings.tsx and wire to PUT /admin/settings:
Core toggles:
REQUIRE_API_KEY
ENFORCE_PUBLIC_HTTPS
PUBLIC_API_URL (show only for Phaxio)
FAX_DISABLED (label: “Disable backend send (Test mode)”)
Backend-specific:
Phaxio: PHAXIO_API_KEY, PHAXIO_API_SECRET (masked), callback URL (read-only derived but allow override), PHAXIO_VERIFY_SIGNATURE
Sinch: SINCH_PROJECT_ID, SINCH_API_KEY, SINCH_API_SECRET (masked)
SIP: ASTERISK_AMI_HOST, ASTERISK_AMI_PORT, ASTERISK_AMI_USERNAME, ASTERISK_AMI_PASSWORD (masked input), SIP_STATION_ID
Inbound:
INBOUND_ENABLED
INBOUND_TOKEN_TTL_MINUTES
INBOUND_RETENTION_DAYS
Backend-specific inbound: Phaxio signature verification; Sinch inbound Basic user/pass and HMAC secret; SIP ASTERISK_INBOUND_SECRET
Storage:
STORAGE_BACKEND local|s3
For s3: S3_BUCKET, S3_REGION, S3_PREFIX, S3_ENDPOINT_URL, optional S3_KMS_KEY_ID
Rate limits:
MAX_REQUESTS_PER_MINUTE
INBOUND_LIST_RPM, INBOUND_GET_RPM
Restart hint:
After PUT /admin/settings, back end returns _meta.restart_recommended. If true, show “Restart recommended”. If ADMIN_ALLOW_RESTART=true, show “Restart” button that POSTs /admin/restart.
UX constraints:
Hide backend-specific controls for other backends.
Secrets inputs accept new values; keep GET-masked values; never echo full secrets.
Deliverables:
Add missing UI fields and map them to updateSettings(...).
Confirm GET /admin/settings masks secrets.
Phase 4: Diagnostics → Tests (run from UI)
Goal: Operators can run common tests from UI without shell. Implement new admin endpoints that replicate the scripts’ logic safely.
Add new endpoints under /admin/tests/*:
Always require admin auth: Depends(require_admin)
Do not run shell scripts. Use Python and existing code paths.
Return JSON: { passed: boolean, steps: Array<{ name: string; ok: boolean; detail?: string }>, details?: object }
Handle errors by returning 200 with passed:false and an error string in details, except for 401 (unauthorized).
Endpoints:
POST /admin/tests/check-env
Input: none
Steps:
Read effective settings
Check required env presence per backend:
Phaxio: PHAXIO_API_KEY/SECRET, PUBLIC_API_URL; if ENFORCE_PUBLIC_HTTPS=true, reject http unless localhost; PHAXIO_VERIFY_SIGNATURE
Sinch: presence of SINCH_PROJECT_ID, SINCH_API_KEY/SECRET
SIP: ASTERISK_AMI_* present; password not changeme
Inbound presence checks when INBOUND_ENABLED=true; S3 config if STORAGE_BACKEND=s3
Output: per-check booleans with messages
POST /admin/tests/send-fax-sim
Input: { to?: string } optional
Steps:
If FAX_DISABLED=true: create dummy job and run conversion (TXT→PDF) only; transition queued→in_progress→SUCCESS with no network
Else backend is phaxio or sinch: reject here with message: “This test is non-destructive; use the Send tab for a real send or enable FAX_DISABLED”
Output: job id, status transitions
POST /admin/tests/inbound-internal-sim
Preconditions: INBOUND_ENABLED=true, ASTERISK_INBOUND_SECRET set
Steps:
Create small temp TIFF placeholder
POST to /_internal/asterisk/inbound with secret header
Reuse admin context to immediately list inbound (do not mint a real API key unless necessary)
GET /inbound and GET /inbound/{id}/pdf and read into temp file
Output: created inbound id, list count change, downloaded=true
POST /admin/tests/phaxio-callback-sim (backend=phaxio)
Preconditions: PHAXIO_API_SECRET present
Steps:
Create a dummy job in DB (queued) with a random job_id
Build minimal callback body, compute HMAC with PHAXIO_API_SECRET, call /phaxio-callback?job_id=<job_id> with X-Phaxio-Signature
Verify job status transitions to SUCCESS
Output: job_id, callback_accepted, final_status
POST /admin/tests/sinch-inbound-sim (backend=sinch)
Preconditions: inbound enabled
Steps:
Build minimal JSON payload { id, from, to, file_url? }
If SINCH_INBOUND_BASIC_USER set, pass Basic header; if SINCH_INBOUND_HMAC_SECRET set, compute HMAC header
POST to /sinch-inbound
Confirm inbound list increases and last item matches
Output: created inbound id, auth_checks_used, list_count_change
POST /admin/tests/mcp-status
Try GET http://localhost:3001/ (Node HTTP): expect { status:'ok', transport:'streamable-http', server:'faxbot-mcp' }
Try GET http://localhost:3002/ (Node SSE): expect { status:'ok', transport:'sse' }
Try GET http://localhost:3003/ (Python SSE): expect { status:'ok', transport:'sse' }
If ports are not mapped (profiles not running), return ok:false, skipped:true, and guidance to start with compose --profile mcp
Example FastAPI signatures (pseudocode):
# api/app/main.py
@app.post("/admin/tests/check-env")
def admin_test_check_env(info=Depends(require_admin)) -> dict: ...

@app.post("/admin/tests/send-fax-sim")
def admin_test_send_fax_sim(info=Depends(require_admin), body: SendSimBody | None = None) -> dict: ...

@app.post("/admin/tests/inbound-internal-sim")
def admin_test_inbound_internal_sim(info=Depends(require_admin)) -> dict: ...

@app.post("/admin/tests/phaxio-callback-sim")
def admin_test_phaxio_callback_sim(info=Depends(require_admin)) -> dict: ...

@app.post("/admin/tests/sinch-inbound-sim")
def admin_test_sinch_inbound_sim(info=Depends(require_admin)) -> dict: ...

@app.post("/admin/tests/mcp-status")
async def admin_test_mcp_status(info=Depends(require_admin)) -> dict: ...
Admin UI wiring:
Add in api/admin_ui/src/api/client.ts:
export type AdminTestResult = {
  passed: boolean;
  steps: Array<{ name: string; ok: boolean; detail?: string }>;
  details?: Record<string, any>;
};

export async function runAdminTest<T extends object = Record<string, unknown>>(
  name: string,
  body?: T
): Promise<AdminTestResult> {
  return http.post(`/admin/tests/${name}`, body ?? {});
}
Diagnostics page: create a “Tests” section with buttons to call:
check-env, send-fax-sim, inbound-internal-sim, phaxio-callback-sim (if backend=phaxio), sinch-inbound-sim (if backend=sinch), mcp-status
Render results as a card: pass/fail chips per step and a raw JSON viewer with copy/download.
Phase 5: Inbound inbox polish
Goal: Comfortable operations without terminal.
Steps:
Filtering: add optional filters to Inbound page using GET /inbound?to_number=&status=&mailbox=
Token TTL hint: display from GET /admin/settings limits.pdf_token_ttl_minutes
Rate limits: show inbound rpm from GET /admin/config (INBOUND_LIST_RPM, INBOUND_GET_RPM)
Download remains via GET /inbound/{id}/pdf using API key auth
Deliverables:
Minimal filters UI
TTL and rpm hints
Phase 6: Send flow safety
Goal: Respect file constraints in UI and map errors clearly.
Steps:
Enforce file type and size client-side:
Accept only application/pdf and text/plain
Enforce 10 MB limit using limits.max_file_size_mb from GET /admin/settings
Error mapping:
Show specific messages for 413 (too large) and 415 (unsupported)
After successful send: show job id and navigate to Jobs with pre-filled search for that id
Deliverables:
Simple client-side size/type guard
Improved error messages for 413 and 415
Phase 7: MCP diagnostics page
Goal: Provide a non-client status page for MCP services.
Steps:
Add “MCP” sub-panel under Diagnostics:
Show which MCP containers are exposed (3001 Node HTTP, 3002 Node SSE, 3003 Python SSE)
Provide a “Check” button to call POST /admin/tests/mcp-status
Display results with labels: “HTTP (Node, 3001)”, “SSE (Node, 3002)”, “SSE (Python, 3003)” with OK/Fail/Not running
Include reminder: “MCP HTTP/SSE JSON limit is 16 MB (base64 overhead). REST API raw file limit is 10 MB. Prefer MCP stdio + filePath for desktop assistants.”
Auth notes (copy only):
Node HTTP: MCP_HTTP_API_KEY required
SSE services use OAuth2/JWT specifics (test only checks liveness on /, not full OAuth)
Deliverables:
UI panel and client.ts call to new tests endpoint
Phase 8: Documentation updates
Goal: Update docs so operators don’t need the terminal beyond docker compose up -d.
Update:
docs-site/docs/admin-console/index.md
“Run Tests from the Console” section summarizing /admin/tests/* and where to find them
Link to backend-specific guides; emphasize backend isolation
Add “Restart API” note and ADMIN_ALLOW_RESTART=true
docs-site/docs/SCRIPTS_TESTS_INDEX.md
For each shell script, note its equivalent Admin Console test
PHAXIO_SETUP.md, SINCH_SETUP.md, SIP_SETUP.md
Add a “Configure via Admin Console” short path (Wizard + Apply + Diagnostics) before deeper manual steps
MCP_INTEGRATION.md
Add “Console MCP Checks” subsection; explain checks are shallow liveness, not a client
Deliverables:
PRs to these docs with short, operator-friendly guidance
Phase 9: QA and acceptance matrix
Run these without using a terminal except to start Docker:
Boot:
docker compose up -d
Admin Console reachable at /admin/ui
Admin login:
Use API_KEY from .env, see Dashboard
Wizard:
Phaxio: enter creds, set public API URL, validate OK, Apply & Reload, export .env
Sinch: enter project ID + keys, validate presence OK, Apply & Reload, export .env
SIP: enter AMI config, validate AMI reachable or actionable errors, Apply & Reload, export .env
Settings:
Toggle “Require API Key”, “Enforce HTTPS”, “Test Mode (FAX_DISABLED)”
Enable Inbound, set token TTL, retention, storage s3, validate presence checks
Diagnostics and Tests:
Run Diagnostics; see system checks (Ghostscript, DB, data dir), backend posture, storage posture
Run Tests: check-env passes; inbound-internal-sim creates and downloads PDF; phaxio-callback-sim (phaxio) flips job to SUCCESS; sinch-inbound-sim (sinch) records an inbound; mcp-status shows OK or Not running with instruction
Send:
Upload 12 MB PDF: UI blocks with size error (no network call)
Upload 9 MB PDF and send: job created; appears in Jobs
Inbound:
List inbound; download a recent item
Restart:
If ADMIN_ALLOW_RESTART=true, click “Restart API”, container restarts, Admin Console comes back
Implementation details and examples
Admin Tests: patterns to follow
Use Depends(require_admin) on each new /admin/tests/*
For calls to own API (e.g., Phaxio callback), prefer direct function calls; use loopback HTTP only if necessary
Structured JSON shape:
{
  "passed": true,
  "steps": [
    { "name": "checked_phaxio_keys", "ok": true },
    { "name": "public_api_url_https", "ok": true, "detail": "ENFORCE_PUBLIC_HTTPS=true; https OK" }
  ],
  "details": { "backend": "phaxio" }
}
/admin/tests/check-env pseudocode:
settings = get_effective_settings()
steps = []

if settings.backend == "phaxio":
    steps.append(step("phaxio_api_key", bool(settings.PHAXIO_API_KEY)))
    steps.append(step("phaxio_api_secret", bool(settings.PHAXIO_API_SECRET)))
    steps.append(step("phaxio_verify_signature", settings.PHAXIO_VERIFY_SIGNATURE is True))
    url_ok = is_https_or_localhost(settings.PUBLIC_API_URL) if settings.ENFORCE_PUBLIC_HTTPS else True
    steps.append(step("public_api_url_https", url_ok, detail=...))

elif settings.backend == "sinch":
    steps += [
        step("project_id", bool(settings.SINCH_PROJECT_ID)),
        step("api_key", bool(settings.SINCH_API_KEY)),
        step("api_secret", bool(settings.SINCH_API_SECRET)),
    ]

elif settings.backend == "sip":
    steps += [
        step("ami_host", bool(settings.ASTERISK_AMI_HOST)),
        step("ami_port", bool(settings.ASTERISK_AMI_PORT)),
        step("ami_user", bool(settings.ASTERISK_AMI_USERNAME)),
        step("ami_password_not_default", settings.ASTERISK_AMI_PASSWORD != "changeme"),
        step("station_id", bool(settings.SIP_STATION_ID)),
    ]

if settings.INBOUND_ENABLED:
    steps.append(step("inbound_secret_present", has_inbound_secret_for_backend(settings)))
    if settings.STORAGE_BACKEND == "s3":
        steps += [step("s3_bucket", ...), step("s3_region", ...), step("s3_endpoint_url", ...)]

passed = all(s.ok for s in steps)
return {"passed": passed, "steps": serialize(steps), "details": {"backend": settings.backend}}
/admin/tests/mcp-status pseudocode:
results = {}
for name, url in {
  "node_http": "http://localhost:3001/",
  "node_sse":  "http://localhost:3002/",
  "python_sse":"http://localhost:3003/",
}.items():
    try:
        r = await http_get(url, timeout=1.5)
        ok = is_expected_mcp_response(name, r.json())
        results[name] = {"ok": ok, "status": r.json()}
    except ConnectionRefusedError:
        results[name] = {"ok": False, "not_running": True, "suggestion": "Start with: docker compose --profile mcp up -d"}
    except Exception as e:
        results[name] = {"ok": False, "error": str(e)}

steps = [{"name": k, "ok": v.get("ok", False)} for k, v in results.items()]
return {"passed": all(s["ok"] for s in steps), "steps": steps, "details": results}
Admin UI wiring guidelines
Add runAdminTest as shown above in client.ts
Diagnostics Tests panel:
Render a button group calling: runAdminTest("check-env"), etc.
Show per-step Chips: green for ok, red for fail
Provide JSON viewer with copy/download
Backend isolation:
Read settings.backend.type from GET /admin/settings
Conditionally render Phaxio only tests/settings when backend=phaxio; similarly for Sinch/SIP
Error handling:
Prefer 200 with passed:false and details.error for most failures
Show friendly mapping for HTTP 413 and 415 in Send UI
Secrets handling
GET /admin/settings returns masked secrets; never attempt to unmask
Updates are one-way: operator inputs new values → PUT /admin/settings → POST /admin/settings/reload
HIPAA vs non‑PHI defaults
Wizard defaults for healthcare: REQUIRE_API_KEY=true, ENFORCE_PUBLIC_HTTPS=true, PHAXIO_VERIFY_SIGNATURE=true, AUDIT_LOG_ENABLED=true
Provide clearly labeled relaxed toggles for non‑PHI development
Out-of-scope (for now)
Running arbitrary shell scripts from UI
Full MCP tool invocation from UI (Admin Console is not an MCP client)
Multi-instance cluster orchestration
Next steps for you
Complete Phases 1–3: close setup and settings gaps
Implement Phase 4 endpoints and Tests UI
Polish Inbox, Send checks, MCP diagnostics (Phases 5–7)
Update docs (Phase 8) and run the acceptance matrix (Phase 9)
Do/Don’t checklist
Do: Add only new endpoints under /admin/tests/*
Do: Keep backend isolation at all times
Do: Return structured JSON; avoid 500s where possible
Don’t: Rename existing endpoints or change auth semantics
Don’t: Echo secrets back to UI
Don’t: Require terminal steps other than docker compose up -d
Short example of UI usage (Tests panel button handlers):
const onCheckEnv = async () => setResult(await runAdminTest("check-env"));
const onSendSim = async () => setResult(await runAdminTest("send-fax-sim"));
const onInboundSim = async () => setResult(await runAdminTest("inbound-internal-sim"));
const onPhaxioCallback = async () => setResult(await runAdminTest("phaxio-callback-sim"));
const onSinchInbound = async () => setResult(await runAdminTest("sinch-inbound-sim"));
const onMcpStatus = async () => setResult(await runAdminTest("mcp-status"));
If you need exact function names or file locations to start from, prefer:
Back end: add endpoints in api/app/main.py
UI client: api/admin_ui/src/api/client.ts
Diagnostics UI: api/admin_ui/src/components/Diagnostics.tsx (or equivalent diagnostics component)
Settings UI: api/admin_ui/src/components/Settings.tsx
Wizard: api/admin_ui/src/components/SetupWizard.tsx
I can also draft precise FastAPI function skeletons and the client.ts and Diagnostics call sites for the new tests endpoints if you want to accelerate implementation.
Ensured the plan is explicit for a junior dev, avoids breaking changes, isolates backends, and mirrors scripts as UI-run tests.
Added endpoint specs, example payloads, pseudocode, and UI wiring guidance for all new “Tests” features.