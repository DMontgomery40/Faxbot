# Runtime foundation verification — October 2, 2026

Status: implemented, locally verified and independently approved at `c17e10c9`; no Critical or Important findings in the bounded task. The full Faxbot refresh remains active and has not been deployed.

## Source and image identities

- Task base: `73e68d7b3c080823f35c80c0ab42371016d2ca5f`.
- Product implementation and image source: `a8ca0ac966179b9f82a64ff044dc47bc88833490`.
- CI-only follow-up: `c17e10c9664aa7597948969030066427672f5b75`. This changes no image inputs.
- API image: `sha256:af7b10ff55e1da8dfc4d87a52d1025068aa41517ca6705604798943530a5a671`.
- Standalone Python SSE image: `sha256:415c806b824f8e22921dd4b50d00f41607e13b38c38ccd8d8fc78ca24ebd16db`.

Both images were built from a clean tracked archive using the actual Dockerfiles. Local proof used only loopback ports, synthetic documents and disposable databases/volumes, with external fax transmission disabled. Owned proof containers/volumes were removed after shutdown.

## Implemented behavior

API and Python MCP dependencies resolve together under committed constraints, with fatal `pip check` in both Dockerfiles and API CI. The original broken image combined incompatible FastAPI and Starlette versions through separate installs. The UI builder now uses Node 24 LTS; UI application dependencies remain unchanged in this slice.

The API uses supported lifespan handling. Cleanup and AMI work are owned and closed on shutdown; enabled MCP transports must initialize successfully, enter their child lifespans and release their resources. A fresh MCP HTTP session manager supports each application lifespan. Standalone stdio/HTTP/SSE package entrypoints install and start outside the repository.

AMI connection/login recursion is removed. A supervised connection loop owns reads, reconnection and closure, and only a successful bounded Login response establishes connectivity. Denied/missing login responses no longer pass the one-off connection probe. Existing Originate fields and FaxResult handling remain; durable delivery is a subsequent required subsystem.

A narrow compatibility adapter gives the pinned MCP SDK's SSE handler a proper ASGI response lifetime, eliminating a duplicate response on disconnect. OAuth rejection remains enforced. The adapter must be revisited when upgrading the SDK.

## Recorded verification

- Fresh macOS Python 3.11 combined dependency installation: `pip check` passes; **125 API tests pass in 18.06 seconds, zero warnings**.
- Actual Linux arm64 image dependency environment and Ghostscript, with committed tests/source: **125 tests pass in 16.97 seconds, zero warnings**.
- Both built images pass `pip check`, start and shut down cleanly. API health/readiness return 200. Startup/shutdown logs contain no errors, warnings or destroyed-task reports.
- Authenticated TXT and two-page PDF upload/download preserve original markers and valid PDF bytes; missing/wrong upload credentials and unauthenticated document retrieval return 401. Real Ghostscript produces nonblank one-/two-frame Group 4 TIFFs.
- Actual MCP SDK clients initialize and enumerate preserved tools over embedded HTTP and SSE. Installed package stdio/HTTP entrypoints complete real protocol initialization; standalone OAuth SSE rejects unauthenticated access.
- Primary independent check calls `send_fax` and `get_fax_status` through MCP HTTP, reads the same job through authenticated HTTP, and downloads a PDF retaining every original text line. Job `3323596b017e401fb2bc0646f7535b62`; observed state `queued` because dispatch is disabled. This demonstrates tool-to-API-to-document behavior, not real fax delivery.
- Synthetic AMI success, rejection, missing response, reconnect, event dispatch and cancellation/closure are covered. No real Asterisk/provider hardware connection was claimed.
- Hosted CI has not yet run for this branch. CI installation was repaired; local equivalent success is recorded separately.

Full reports, fixed diff package, reproduction scripts, precise commands, image metadata and logs are retained in `.superpowers/sdd/2026-10-02-faxbot-runtime-foundation/` in the integration worktree.

## Lifecycle decision

Retain sse-starlette 3.5.0 and its intentional single server-lifetime shutdown watcher. Its current release fixes a stopped-server regression. Tests require every Faxbot/MCP session resource to close on app lifespan exit, identify and bound that exact remaining framework watcher across repeated lifespans, and prove actual server process shutdown. No private global task cancellation or broad leak exemption was added. If this distinction is wrong, a framework leak could be missed; the explicit task-count/session checks and process-shutdown proof guard that risk. [Upstream release](https://github.com/sysid/sse-starlette/releases/tag/v3.5.0), [exact watcher source](https://raw.githubusercontent.com/sysid/sse-starlette/v3.5.0/sse_starlette/sse.py).

## Whole-product gates still open

The unchanged UI dependency audit reports 12 findings, including one critical and eight high. Resolve these in the UI/dependency work. Image base tags are maintained tags; exact tested output IDs above identify this proof. Other architectures, hosted CI, production deployment, real fax receipt, external OAuth issuer success, versioned migration/adoption, durable jobs, complete RBAC, clients/TestFlight and automatic docs publication remain required whole-product gates.

## Review follow-up

The independent reviewer recorded one minor diagnostic improvement: AMI reconnect warnings should distinguish a safe failure category (authentication, timeout or connection failure) without exposing credentials. It is tracked for the provider/diagnostics slice and final branch review; runtime acceptance is approved.
