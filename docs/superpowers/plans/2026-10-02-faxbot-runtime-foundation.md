# Faxbot Runtime Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Primary owns architecture and whole-product integration.

**Goal:** The actual Faxbot API image installs one coherent, reproducible Python dependency set and starts successfully with supported API and optional Python MCP transports. Lifecycle startup/shutdown owns its background resources.

**Architecture:** Retain Python 3.11, FastAPI, SQLAlchemy, existing successful HTTP contracts, and the embedded Python MCP integration. Upgrade compatible dependencies based on current official package metadata; resolve API and MCP requirements together instead of allowing the second pip install to break the first. Replace deprecated API startup hooks with supported lifespan handling, including embedded transport lifetime. This is a runtime foundation repair, not the later schema/worker/RBAC/client redesign.

**Spec:** `docs/superpowers/specs/2026-10-02-faxbot-refresh-design.md`.

## Global Constraints

- Use gpt-6.1-sol at xhigh for bounded implementation and independent review; do not spawn helpers.
- Preserve existing successful public request/response contracts and real document conversion.
- FAX_DISABLED suppresses external transmission only; all proof uses synthetic documents, temporary databases and no provider credentials.
- No production changes, publication, external messages or real fax sends in this slice.
- Do not silently omit an explicitly enabled MCP transport after import/startup failure. Disabled transports remain optional.
- Keep API and Python MCP requirement declarations compatible and install them in one resolver transaction. Dependency conflicts and startup failures must fail verification, never be ignored.
- Retain Python 3.11 support. Choose compatible supported stable packages using primary package/documentation sources; record exact resolved versions and source URLs. Avoid unnecessary stack changes.
- Primary owns schema/configuration and durable worker design in later required slices; do not change database adoption/migrations or fax delivery semantics here.
- Full product completion still requires complete RBAC, clients including TestFlight, provider/delivery verification, generated docs and deployment.

## File Responsibilities

- `api/requirements.txt`, `python_mcp/requirements.txt`, `python_mcp/pyproject.toml`: coherent direct runtime declarations, shared resolution/constraints where necessary.
- Dependency lock/constraint file and regeneration instructions if introduced: reproducible combined production Python set without platform-specific macOS-only pins.
- `api/Dockerfile`, `python_mcp/Dockerfile.sse`: correct dependency installation and supported build image; package checks are fatal. Preserve UI build and real Ghostscript.
- `api/app/main.py`: API lifespan and embedded MCP mount/lifespan wiring only; do not refactor unrelated routes.
- Small new runtime/lifespan helper if needed: owns tasks and cleanup explicitly; no swallowed startup failures.
- `python_mcp/server.py`, `http_server.py`, `stdio_server.py`: only changes needed for supported runtime initialization/transport lifecycle, retaining advertised interfaces.
- `api/tests/test_runtime.py` (new), affected existing tests: meaningful startup/shutdown and actual MCP protocol initialization/tool enumeration regressions.
- Runtime evidence/report: exact source versions, failed baseline, tests and Docker observations.

### Task 1: Repair and verify the combined runtime

**Base:** record current HEAD immediately before dispatch. Confirmed baseline container failure: FastAPI 0.112.2 + independently installed Starlette 1.7.0; pip check fails, app import raises Router.__init__ unexpected on_startup. Existing full API baseline is 109 passed with four lifecycle warnings at 6a0afbba.

- [x] Inspect current MCP transport implementation, lifecycle, packaging and Dockerfiles before choosing versions. Verify package/API compatibility with official metadata/documentation and record sources.
- [x] Capture the existing dependency/startup failure as a reproducible failing check. Add failing behavior tests for clean API lifespan entry/exit, background task ownership/cleanup, and enabled MCP protocol initialization/tool listing. Actual SDK transport handshake evidence must supplement any seam tests. Preserve existing tool names/contracts.
- [x] Resolve and pin a compatible common API/MCP dependency set in one installation transaction. Keep standalone Python MCP metadata consistent. Use a fresh environment for evidence; do not mutate the primary document proof environment `.venv`.
- [x] Replace deprecated on_event startup with supported lifespan. Preserve initialization sequence and own cleanup/AMI tasks through shutdown. Ensure failed enabled MCP startup is visible and mounted session managers enter/exit their lifespan as required. Avoid duplicate background loops across repeated lifespan contexts.
- [x] Make actual Dockerfiles use coherent install plus fatal pip check; move EOL Node 18 UI builder to a supported Node LTS after checking official compatibility. Do not change frontend application dependencies in this task; UI audit remediation is a later bounded task.
- [x] Run the full API suite with the new runtime, temporary DB/data, disabled external sending and real Ghostscript. Run runtime/MCP checks against actual transports, not solely imports. Record warnings and unverified edges.
- [x] Build the actual API Dockerfile from a clean tracked snapshot, run container pip check, start it on loopback with synthetic DB/data, verify health, authenticated synthetic upload/download contents and enabled MCP initialize/tools list. Use explicit Docker context `colima-faxbot-refresh`. Do not include `.venv`, scratch data or secrets in the context. The existing image `faxbot-refresh:47a34e65` may be used to reproduce the old failure.
- [x] Verify clean shutdown and no surviving runtime-owned tasks/connections in covering tests. Run git diff --check, review the diff, commit the bounded change with factual subject and @codex review in body. Write report with base/head, exact commands/results and remaining findings.

## Preflight Integration Check

| Pair | Dependency | Resolution |
|---|---|---|
| API and Python MCP | FastAPI/Starlette/AnyIO/Pydantic shared interpreter | Single compatible resolved install; standalone metadata remains compatible |
| API and mounted MCP | ASGI mount does not automatically manage child lifespan | Explicitly enter transport lifecycle where required; handshake verifies actual behavior |
| API startup and shutdown | AMI/cleanup tasks started by API | Retain task handles, cancel/await cleanly, avoid duplicated callbacks/loops |
| Runtime and migrations | init_db currently legacy | Preserve existing behavior for now; versioned adoption handled by primary next |
| Runtime and document slice | Real conversion plus preserved successful API shape | Full 109-test baseline and actual artifact proof on upgraded environment/image |

## Review and Acceptance

Primary dispatches independent review against the fixed full task range, brief, report and global constraints. Address Critical/Important findings before acceptance. Source/tests, image build, image startup and deployment are distinct claims; this task requires the first three plus local actual-image HTTP/MCP proof. It does not count as production deployment or whole-product completion.

## Completion and implementation clarifications

Independently approved at c17e10c9, with actual image source a8ca0ac9 and125-test macOS/Linux results. See ../../modernization/evidence/2026-10-02-runtime-foundation.md. Primary-approved necessary expansions included owned AMI authentication/reconnect/closure, the one-line Pydantic field-access update, a pinned SSE ASGI compatibility adapter preserving OAuth, standalone package entrypoints, safe build-context exclusions, HTTPX2 alongside HTTPX for current TestClient, and the combined dependency installation in CI.

The exact sse-starlette3.5 server-lifetime watcher is accounted for separately from application resources; no arbitrary surviving tasks are allowed. The decision, upstream sources, cost if wrong and tests are recorded in the evidence. No dependency downgrade, private global task cancellation, warning suppression or disabled origin protection was used.
