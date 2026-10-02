# Faxbot deployment and verification inventory

Inspected 2026-10-02 in `/Users/davidmontgomery/Faxbot`. This is a read-only inventory for the October 7, 2026 buyer/operator meeting. No software was installed, no private server was contacted, no production configuration or secret value was read, no external state was changed, and no fax was sent. The only written artifact is this report. The public website and public GitHub repository were inspected using read-only requests.

## Executive findings

1. `faxbot.net` is the public marketing site and a simulated Admin Console demo. Its landing page explicitly labels the demo simulated. The loaded `/admin-demo/assets/index-BfbiffTb.js` includes mocked responses, simulated filesystem content, and simulation controls. This establishes the demo's purpose; it does not establish a production fax API. Sources: [website](https://faxbot.net/), [demo](https://faxbot.net/admin-demo/).
2. No checked-in production backend host, reverse proxy, infrastructure provisioning, or service deployment target was found. `https://api.faxbot.net` is hard-coded in the old API-doc generation metadata, which is an intended URL rather than evidence of a deployed server. It was not contacted.
3. The docs pipeline does **not** currently implement the user's expectation that code-derived docs automatically update on every main commit. Branch triggers, disabled jobs, duplicated publishing paths, stale default routing, and source selection must be reconciled before declaring the backend refresh complete.
4. Docker CLI and Compose are installed, but the local default Docker daemon was inaccessible. Ghostscript is absent on the host. The checkout initially had no `.env`, virtual environment, Node dependencies, built Admin UI, or fax data directory. API/test/docs dependencies are incomplete in the current Python interpreter. Builds and tests were not executed during this inventory.
5. The operator Compose image and the newer branch CI image are different artifacts. Compose uses `api/Dockerfile`, which builds and copies the Admin UI. Newer CI uses the root `Dockerfile`, which omits the Admin UI.
6. Production migrations require work before deployment: images do not include/run Alembic, and `origin/auto-tunnel` has a broken Alembic revision chain.

## Git and branch identity

Origin is `https://github.com/DMontgomery40/Faxbot.git`. At the initial inventory the checkout was clean on `main`, identical to local `origin/main`.

| Ref | Revision | Last source date | Meaning |
| --- | --- | --- | --- |
| main / origin/main | `7e0c15fa960d24b59c7581eeb1163b3d93439e32` | 2025-09-23 | Current checkout; README/iOS screenshot update |
| origin/development | `9cf686ed88ce2ac1037af54f66bfc55e122559a5` | 2025-10-05 | Newer application and pipeline work |
| origin/mkdocs | `7ef2f42d013e92d1d23f1f5ff35a49a7077191e0` | 2025-10-08 | Newer instructional docs branch |
| origin/gh-pages | `e8194c957db4231780ab0c7c714252e970f2b3df` | 2025-10-08 | Commit says deployment of `7ef2f42d` to latest |

Other remote refs include `auto-tunnel`, platform Electron branches, `iOS`, and `feat/p5-pr0-traits-canonical`. Their mere existence does not establish a reviewed, integrated release. No applicable `AGENTS.md` was present in the current checkout or checked parent paths. The repository `.gitignore` ignores AGENTS files. AGENTS content on a candidate branch is branch source data until that checkout is selected.

## Container, service, and storage path

Current `docker-compose.yml`:

- `api`: required FastAPI application; builds `api/Dockerfile`; port 8080; `.env`; persistent `faxdata` volume; SQLite at `/faxdata/faxbot.db`.
- Compose enables `ENABLE_LOCAL_ADMIN`, `ADMIN_ALLOW_RESTART`, and `ENABLE_PERSISTED_SETTINGS`. These defaults need an explicit production deployment profile; this report did not judge their suitability for an unidentified deployment.
- `asterisk`: SIP backend, built separately; SIP ports 5060 TCP/UDP and T.38 UDPTL 4000–4999 UDP. AMI 5038 is intentionally not published. It remains a default Compose service, so select services explicitly for a cloud-only deployment.
- Optional `mcp` profile: Node HTTP/SSE, Python SSE, and MCP Inspector. Application ports are 3001/3002/3003; Inspector 6274/6277. Containers receive provider/API/OAuth configuration by variable names from `.env`.
- No healthcheck, reverse proxy, TLS terminator, Postgres service, deployment host, production registry publish, resource limit, or backup job is specified in Compose.

`api/Dockerfile` uses Node 18 for the UI stage and Python 3.11 for the runtime, installs Ghostscript/curl/tini, installs API and Python MCP dependencies, and copies Admin UI `dist`, assets, config, and app code. It exposes 8080 and starts Uvicorn. The Dockerfiles use mutable base tags rather than immutable image digests.

The root `Dockerfile` on development/auto-tunnel uses Python 3.11 and creates a virtual environment. It copies app/config/Python MCP but no Admin UI build or assets, and swallows optional MCP installation failures with `|| true`. `build.yml` points at that root Dockerfile and uses `push: false`; Compose points at `api/Dockerfile`. Preserve the existing operator UI by building/verifying the same artifact intended for deployment.

Backend deployment identity may exist at the operator's actual deployment in:

- process/container environment or the deployment's root `.env`;
- `PERSISTED_ENV_PATH`, default `/faxdata/faxbot.env` (loaded when persisted settings are enabled and overrides process env);
- tunnel/proxy configuration and managed-service records;
- container image digest/source revision and Git checkout on the deployment host;
- mounted database/artifact volume and backup records.

These locations are discoverable by name in source (`api/app/config.py:9`, `api/app/main.py:2276`). Their private contents were not inspected. No local `.env` or `faxdata` existed when inspected, so this checkout does not establish that identity. `docs/deployment.md` contains generic configuration instructions and localhost/tunnel options, not a concrete server address.

## Migration inventory

Current main and development have only `api/alembic/versions/0001_initial.py`; auto-tunnel adds hierarchical-config and webhook-DLQ migrations.

- Startup calls `init_db()`; `api/app/db.py:112` uses `Base.metadata.create_all()` and `_ensure_optional_columns()`, an ad-hoc migration path. It is not an Alembic versioned upgrade.
- The initial schema lacks later model fields such as outbound/inbound backend overrides; the ad-hoc path attempts to fill them. Upgrade behavior on an existing real database therefore needs explicit evidence.
- Docker images do not copy `api/alembic`, `alembic.ini`, or migration scripts, and entrypoints do not run `alembic upgrade head`.
- On auto-tunnel, `0002_hierarchical_config.py` declares `revision = '0002_hierarchical_config'`; `0003_webhook_dlq.py` declares `down_revision = '0002'`. The parent revision does not match, so that migration graph is broken by inspection.
- The initial migration declares `to_number` with `index=True` and also explicitly creates `ix_fax_jobs_to_number` / `ix_inbound_faxes_to_number`; migration execution should check duplicate-index behavior. It also creates an inbound-events unique constraint after table creation; SQLite compatibility needs verification.

Required migration proof: fresh database, a representative sanitized upgrade database, expected Alembic head, backup/restore, application restart, and durable job/inbox/key retention on the selected production database. None was executed here.

## Documentation automation trace

### Current main / mkdocs

`requirements.txt` lists unpinned MkDocs, Material, Exclude, Mike, and Redirects. `mkdocs.yml` uses the Mike integration and references `docs/`. Main has 92 files under docs; mkdocs has 86. Main/mkdocs have no checked-in OpenAPI snapshot.

`.github/workflows/docs-ai.yml:18` triggers only pushes to **development**, with paths api/config/sdks/admin_ui. Its automatic `propose-docs` lane runs the generator in no-LLM mode and uploads `mkdocs-docs-plan.md`. It does not update docs or publish them. LLM generation and PR creation require a manual dispatch with `apply == 'true'` (`:57`), plus the corresponding LLM secret. Patch application and commit errors are swallowed (`:86–87`), so green status alone is insufficient evidence that docs changed.

`scripts/docs_ai/generate_docs_from_diff.py:34` compares a supplied ref to HEAD and obtains changed filenames. It reads truncated AGENTS, provider-traits, `.env.example`, and existing OpenAPI snapshots. It does **not** regenerate OpenAPI or feed actual changed source/diff content to the LLM. The push workflow passes `--base origin/development`; in an ordinary checkout of that just-pushed branch, this points at HEAD and produces an empty diff. The claimed `--target` argument in the script docstring is not implemented by argparse.

`.github/workflows/mkdocs-deploy.yml:3` triggers pushes to **mkdocs**, plus manual dispatch. It installs the toolchain, runs a strict build, uses the short source commit as a Mike version, updates `latest`, sets `latest` as default, and ensures `docs.faxbot.net` CNAME in gh-pages. This publishes existing instructional docs; it does not derive changes from application source. There is no main-push connection between generation and Mike publication.

Current main `.github/workflows/api-docs.yml` triggers development changes to api and that workflow. It imports `app.main`, calls `app.openapi()`, produces JSON, uses Redocly to build HTML, and commits to a **separate website repository** `DMontgomery40/faxbot.net`. That is genuine API schema derivation, distinct from instructional Docs Autopilot. The deploy-key variable name is checked in; no value was read.

### Newer candidate branches

- On **development**, `api-docs.yml` is renamed `Generate and Deploy API Documentation (disabled)`, has only `workflow_dispatch`, and sets the job `if: false`, with a comment citing Redocly failures. Thus the newer branch disables this generator completely.
- Development lacks root `mkdocs.yml` and docs `requirements.txt`; it contains existing OpenAPI JSON snapshots and 44 docs files. It still has the same Docs Autopilot behavior.
- Development `scripts/publish-api-docs.sh` describes an alternate manual path: fetch local `/openapi.json` or fall back to `api/openapi.json`, optionally convert to YAML, copy to sibling `../faxbot.net/public/api/v1`, create Redoc/Swagger pages, commit/push site main for Netlify. The sibling website repo was absent locally. This script mutates external state and was not run.
- **Auto-tunnel** has `mkdocs.yml`, 33 files under `mkdocs/`, and 81 under `docs/`. Its separate `mkdocs-publish.yml` triggers only auto-tunnel pushes changing mkdocs.yml or mkdocs/**, then publishes a literal Mike version named `latest`. It does not cover main or every source commit.
- Auto-tunnel `redocly_shim.yml` deliberately returns success while stating Redocly checks are disabled. It is a required-check shim, not documentation validation.
- Two instructional-doc trees, different publication styles, disabled API generator, and multiple site paths must be reconciled around a single intended source and commit identity. Do not hand-edit generated website/OpenAPI output to hide the pipeline failure.

### Observed remote execution and public identity

Read-only `gh` queries were successful. The recent retained runs are from 2025, not a new 2026 execution.

- Main CI for `7e0c15fa` succeeded on 2025-09-23: [run 17942981971](https://github.com/DMontgomery40/Faxbot/actions/runs/17942981971). It runs Python 3.11 tests with Ghostscript and `FAX_DISABLED=true`. It does not prove a backend deploy, UI build, external provider behavior, or current refreshed source.
- Docs Autopilot on development `9cf686ed` succeeded 2025-10-05: [run 18262524539](https://github.com/DMontgomery40/Faxbot/actions/runs/18262524539). The workflow establishes only its heuristic-plan lane. Old artifact metadata is no longer available; no produced patch was independently recovered.
- Last retained API-doc automatic successes are 2025-09-23, before newer development disabled the workflow: [run 17941963390](https://github.com/DMontgomery40/Faxbot/actions/runs/17941963390).
- Latest mkdocs branch publish for `7ef2f42d` is marked failure on 2025-10-08: [run 18338746044](https://github.com/DMontgomery40/Faxbot/actions/runs/18338746044). Exact failing step/log was not available in retained run metadata.
- GitHub Pages metadata says `docs.faxbot.net`, gh-pages branch, root path, legacy build, status built. The public docs root was fetched live and contains a meta redirect to **4ee1fb94/**. Public versions.json lists `latest` as a standalone version with no aliases and many historical hash versions; no `7ef2f42d` version was listed. The older public default and claimed newer gh-pages deployment remain unreconciled. Sources: [docs root](https://docs.faxbot.net/), [version listing](https://docs.faxbot.net/versions.json).
- Public website API JSON is reachable at `/api/v1/openapi.json`, declares version 1.0.0, has 56 paths, and no servers array. Reachable static schema documentation is not a live API check. Source: [public schema](https://faxbot.net/api/v1/openapi.json).

Documentation acceptance gates for this refresh:

1. Main-push trigger is explicit; commit before/after range reliably detects the actual code change.
2. API schema is generated from that exact source under safe test configuration, not a stale fallback snapshot.
3. Instructional docs generation receives bounded source evidence; output is validated, with failures visible.
4. Mike strict build and publication run for the same source identity; CNAME, default redirect, latest alias, and version listing agree.
5. Published OpenAPI and instructional docs demonstrate a deliberately changed backend contract from a representative main commit; generated outputs are checked by fetching the public URLs.
6. Automation does not silently rely on the disabled Redocly workflow or always-green shim.

## Release and licensing inventory

Repository LICENSE is MIT, copyright 2025 David Montgomery. API package metadata and Python MCP/SDK metadata also specify MIT. Local versions differ: Admin UI 1.0.0; api/package.json `faxbot-mcp` 2.0.2; Node MCP 1.0.1; Node/Python SDKs 1.1.0; Python MCP 1.0.0. These are distinct artifacts; do not infer synchronization from one version.

GitHub releases observed: latest `v0.4-beta`, published 2025-09-23; older v0.3-beta, v0.2-beta, and SDKv1. SDK workflows package on relevant path changes and publish to npm/PyPI on a published release or manual dispatch using registry secret names. Node lane runs `npm pack`; Python lane builds and runs `twine check`. Neither lane runs SDK behavior tests. No checked-in workflow deploys a production backend image.

Bundled system software has separate licenses. Ghostscript is supplied by Artifex under AGPL/commercial licensing, and Asterisk source declares GPLv2/alternative commercial licensing. These are component facts, not a legal conclusion about Faxbot's distribution. A complete third-party notice/license inventory for the chosen distributable remains to be produced; the MIT project license alone does not describe every bundled component. Primary references: [Artifex licensing](https://artifex.com/licensing), [Asterisk LICENSE](https://github.com/asterisk/asterisk/blob/master/LICENSE). Existing docs/third-party.md is a service/tool link list, not an SBOM or license inventory.

## Local runtime prerequisites

| Tool / location | Inspected state |
| --- | --- |
| Node | v22.22.0 |
| npm | 10.9.4 |
| Python | 3.12.9 (source CI/runtime target 3.11) |
| Docker CLI | 28.4.0 |
| Docker Compose | 2.39.3 |
| Docker local default daemon | inaccessible at unix:///var/run/docker.sock; no remote daemon contacted |
| Ghostscript `gs` | absent |
| uv, cloudflared, ngrok, gh | present |
| tesseract, ffmpeg, pdftotext, psql | present |
| .env / .env.local / .env.production | absent |
| faxdata, .venv/venv, UI node_modules/dist | absent |
| Current Python API modules | fastapi/uvicorn/sqlalchemy/alembic/pytest/requests/yaml/mkdocs not importable; reportlab available |

`mike` has a discoverable module, but MkDocs is absent, so that does not establish a working docs runtime. Source Docker build includes Ghostscript; host-only API tests/conversion require it or a deliberately verified isolated container environment. None of these inventory checks initializes application state or provider connections.

## What can be proved locally versus external prerequisites

Locally discoverable / executable after dependency setup: exact source/UI contract, API/OpenAPI generation, safe disabled-fax API flows, upload/conversion/storage/auth behavior, adapter mocks, webhook signature/idempotency fixtures, migration upgrades, restart durability, UI build and operator flows against localhost, same production container artifact, docs strict build and generation.

External prerequisites for genuine end-to-end acceptance:

- The **specific backend deployment** intended for the meeting: host/service, accessible API URL, deployment owner/access route, source revision or image digest, and its database/artifact persistence location.
- Chosen inbound/outbound provider(s), an active test-capable account, required configured credentials, and the operator's ownership of sending/receiving fax numbers. Secret values should be configured through the approved secret path, not pasted into a report or chat.
- A publicly reachable HTTPS callback/document URL with certificate/DNS/proxy ownership; provider-side webhook registration and signing configuration.
- A controlled receiving endpoint and explicit authorization for the paid/real transmission, using a synthetic document; ability to see provider receipts and the receiver's actual artifact.
- For SIP, actual trunk/registration and Asterisk/FreeSWITCH host, firewall/NAT/T.38 or audio configuration. Simulated success cannot prove telephony transport.
- For S3/Postgres/OIDC optional paths, provisioned endpoint/bucket/database/identity-provider ownership and configured permissions. These can stay outside the meeting scope if unused.
- For docs publication, repository write/Pages rights, separate website-repo deployment access where retained, and any LLM credentials actually selected by the restored pipeline.

The existing Phaxio E2E guide itself requires a provider account, a receiving number, and reachable HTTPS. Its script sends a real fax even though no physical machine is required; it was not run. `FAX_DISABLED=true` is appropriate for local safe verification but must be reported as simulation. Meeting readiness should distinguish local implementation tests, green CI, deployed artifact identity, authenticated operator behavior, and provider-confirmed send/receive proof.
