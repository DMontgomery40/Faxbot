# Faxbot retained product contract inventory

Read-only inventory on 2026-10-02 for the 2026-10-07 completed, verified handoff. No repository changes, credentials, provider network calls, package publication, or installed-app state were inspected. This is a source inventory and acceptance proposal, not an implementation design or a claim that these surfaces run successfully.

## Baselines and interpretation

| Ref | Exact source identity | Meaning |
|---|---|---|
| main / origin/main | 7e0c15fa960d24b59c7581eeb1163b3d93439e32 | Current checkout; liked web console is the visual baseline |
| origin/development | 9cf686ed88ce2ac1037af54f66bfc55e122559a5 | Additional traits/provider/tunnel and test UI work |
| origin/iOS | 5ac8cd5e061f9e0abe9d418df73fec13832366ba | Full SwiftUI app and share extension; not in main |
| origin/electron_macos | 1749b3621b7783520b5cd51d939ee44320f454b8 | Desktop-specific packaging, assets, integration |
| origin/electron_windows | 50be4732f65330159779a81a30308175918f5944 | Windows packaging/integration |
| origin/electron_linux | 00b4ab37f52540719c343c37bc9e4fb764c023fd | Linux packaging/integration |
| origin/feat/p5-pr0-traits-canonical | 1f9b08a9fc57ea65a07f72bfb2399b315d308237 | Much broader partial implementation: config hierarchy, marketplace, identity, events, webhook processing |

The iOS source is recoverable in this repository's remote branch. Its absence from main is a branch-reconciliation/code ownership issue, not an external missing-source prerequisite. Apple signing/account/TestFlight/device access are separate external prerequisites. No branch was checked out or merged during this pass.

No feature removal is implied by classifying a surface as broken, scaffolded, branch-only, or advertised. Those classifications identify explicit repair/verification work and evidence boundaries. They do not justify deleting UI, transports, clients, or documentation promises.

## Web console: preserve this actual main contract

Entry: api/admin_ui/src/main.tsx renders React 18 + MUI App without MSW. api/admin_ui/vite.config.ts sets base /admin/ui/, build dist, dev port 3000, proxies /admin, /fax, /inbound. main.py conditionally mounts /admin/ui when ENABLE_LOCAL_ADMIN=true. The UI is state/tab-driven, not a React Router URL application.

Main top-level tabs: Dashboard, Send, Jobs, Inbox, Settings, Tools. Settings sub-tabs: Setup, Settings, Keys, MCP. Tools sub-tabs: Terminal, Diagnostics, Logs, feature-gated Plugins, Scripts & Tests. Preserve existing responsive mobile drawer, theme toggle, logos/colors, form layout, loading/error/success states, and download/copy affordances. ThemeContext/themes are the visual source; this inventory does not propose redesign.

Login creates AdminAPIClient and validates the key through GET /admin/config. Keys persist in localStorage key faxbot_admin_key and logout removes it. All normal requests use window.location.origin and X-API-Key. Admin authorization accepts bootstrap key or keys:manage. UI access restriction is applied to /admin/ui, not all programmatic /admin endpoints; main permits loopback, plus private IP in container, and rejects forwarded UI requests. App retains stale Dashboard navigation indices: diagnostics calls onNavigate(9), settings calls onNavigate(6), whereas only tabs 0..5 exist. These are repair tasks preserving the intended destination.

### Client methods, paths, response shapes, consumers

The exact main client source is api/admin_ui/src/api/client.ts; types are api/admin_ui/src/api/types.ts. Most extensible config/plugin responses are any, so source-level shape comparison is necessary.

| UI flow / methods | Exact backend contract | Required shape / observable behavior |
|---|---|---|
| Login, Dashboard, settings metadata: getConfig | GET /admin/config | backend, hybrid {outbound,inbound,explicit flags}, allow_restart, branding.docs_base/logo_path, mcp, configured flags, security, limits, inbound, storage; optional v3_plugins |
| Dashboard getHealthStatus/startPolling | GET /admin/health-status | timestamp, backend, backend_healthy, jobs {queued,in_progress,recent_failures}, inbound_enabled, api_keys_configured, require_auth; poll default 5s |
| Settings getSettings/updateSettings | GET/PUT /admin/settings | Nested masked Settings response; PUT flat UpdateSettingsRequest fields. Effective outbound/inbound, credential/storage/security/limit values must agree after update |
| Setup validateSettings | POST /admin/settings/validate | backend, checks, optional test_fax. Main validates Phaxio network auth, Sinch presence only, SIP AMI + gs; other chosen backends have only common writable-directory check |
| Export/persist/reload/restart | GET /admin/settings/export; POST /admin/settings/persist, /admin/settings/reload, /admin/restart | export {env_content,requires_restart,note}; persist {ok,path}; restart permission and metadata must drive UI accurately |
| Send sendFax | POST /fax multipart fields to,file | HTTP 202, FaxJobOut; queued means accepted, not delivered. UI accepts PDF/TXT up to hardcoded 10MB, displays id, clears form on success |
| Jobs listJobs | GET /admin/fax-jobs?status=&backend=&limit=&offset= | {total,jobs}; admin rows use masked to_number, status, backend, pages, sanitized error, created_at,updated_at |
| Jobs getJob/downloadJobPdf | GET /admin/fax-jobs/{id}; GET /admin/fax-jobs/{id}/pdf | detail adds provider_sid,file_name; PDF binary download, no-cache headers |
| Jobs refreshJob | POST /admin/fax-jobs/{id}/refresh | Main supports installed HTTP-manifest providers only. Returns public FaxJobOut with to rather than admin FaxJob type's to_number; UI expects FaxJob. Preserve refresh affordance and fix contract mismatch |
| Inbox listInbound/downloadInboundPdf | GET /inbound; GET /inbound/{id}/pdf | Flat array of metadata; fr,to,status,backend,pages,received_at. Download uses X-API-Key. UI masks numbers and polls 15s; backend public metadata is unmasked |
| Inbox / Setup helpers | GET /admin/inbound/callbacks; POST /admin/inbound/simulate | callback {backend,callbacks}; simulation {id,status}; simulation creates placeholder PDF, so cannot count as document or receive proof |
| API keys | GET/POST /admin/api-keys; DELETE /admin/api-keys/{id}; POST /admin/api-keys/{id}/rotate | list metadata, create {key_id,token}, rotate {token}; actual response may add fields. Reveal created token once; scoped key tests required |
| Diagnostics | POST /admin/diagnostics/run | timestamp,backend,checks,summary {healthy,critical_issues,warnings}; optional test send/status/log correlation in Diagnostics |
| Logs | GET /admin/logs?q=&event=&since=&limit=; GET /admin/logs/tail?q=&event=&lines= | {items,count}, tail optional source. Query/filter/export behavior is product contract |
| MCP settings | GET /admin/config; PUT /admin/settings; POST /admin/settings/reload; fetch configured MCP health path | mcp.sse_enabled/sse_path/require_oauth/oauth/http_enabled/http_path. Embedded transport mounts are established on import, so reload cannot prove mounting changed |
| Plugins (FEATURE_V3_PLUGINS) | GET /plugins; GET/PUT /plugins/{id}/config; GET /plugin-registry | {items}; config {enabled,settings}; update {ok,path}. Main persists config, explicitly says runtime apply is future work |
| HTTP manifests | POST /admin/plugins/http/validate, /install, /import-manifests | validate manifest + credentials/settings/to/file_url/from_number/render_only; install {manifest}; import {items,markdown,source=repo_scrape}; normalized result vs metadata must be clear |
| TunnelSettings | GET /admin/tunnel/status; POST /admin/tunnel/config,/test,/pair | status enabled/provider/status/public_url/local_ip/last_checked/error_message; test {ok,message,target}; pair {code,expires_at} |
| Admin allowlisted actions | GET /admin/actions; POST /admin/actions/run {id} | {enabled,items}; {ok,id,code,stdout,stderr}. Client methods exist but main components do not call them |
| Terminal | WebSocket /admin/terminal?api_key= | Browser JSON input/resize/ping, server ready/output/error/exit/pong. Authenticate bootstrap or keys:manage DB key; preserve interactive resize/reconnect/clear/copy/fullscreen |

Important shape distinctions:

- Public /fax/{id} uses {id,to,status,error,pages,backend,provider_sid,created_at,updated_at}; admin job views use to_number. Keep both observable surfaces compatible.
- /inbound returns a flat list, only supports to_number/status/mailbox filters, hard limits 100; no cursor/limit parameters. MCP currently advertises cursor/limit that backend ignores.
- Status convention expected by UI is queued, in_progress, SUCCESS, FAILED, with UI tolerance for lower-case failed/completed/sending. Main provider/background paths are inconsistent; no enum schema guarantees canonical values.
- README advertises /admin/providers and traits in /admin/config, but these are absent from main. They exist on development/traits branch and need reconciliation, not invention.
- Settings TS type has optional features and S3 details; main GET /admin/settings omits features and several S3 fields. UI feature switches initialize fallback false and hybrid credential visibility still often uses legacy settings.backend.type. Save code only sends some backend-specific credentials (notably ordinary Settings apply lacks its visible Documo/SignalWire/FreeSWITCH fields). SetupWizard has more fields but validates legacy backend even when outbound selection differs. Preserve all controls and verify their effect.
- Main Plugins entries claim enabled based on legacy fax_backend; config and dispatch select effective providers differently. Merely writing a config file is not an applied/usable provider.
- PluginBuilder.tsx exists but is not imported/rendered by main App. It generates scaffold code with TODO transmission and fake success. Python template uses JavaScript booleans rendered into Python and strings/interpolation need validation. Advertised plugin builder is completion work, not a working runtime plugin generator.
- Main Terminal logs the full URL containing api_key in browser console; terminal backend logs input samples. Console/log hygiene needs acceptance validation, especially because README advertises no PHI in logs.

## Public API, artifact, and receive surfaces outside UI

Retain /health and /health/ready; POST /fax, GET /fax/{id}; tokenized GET /fax/{id}/pdf; GET /inbound, /inbound/{id}, /inbound/{id}/pdf. Public file endpoints have artifact expiry/auth semantics distinct from admin downloads. Missing/expired/wrong token, revoked/expired/insufficient-scope key, size/type/number failure, and rate limiting must remain observable errors.

Provider/internal ingress surfaces on main: POST /phaxio-callback (outbound), /phaxio-inbound, /sinch-inbound, /signalwire-callback (outbound), /_internal/asterisk/inbound, /_internal/freeswitch/outbound_result. Receive metadata/file must remain correlated to provider ID and effective inbound backend. Internal endpoints depend on shared volume path + internal secret. No generic inbound provider plugin dispatch, user-facing cancel endpoint, or outgoing job batch endpoint is present in main.

Storage has concrete LocalStorage and S3Storage put/get/delete implementations. Local artifact paths and SQLite persistence are compose-backed /faxdata. S3 is used by inbound storage; admin outbound download still reads a fixed local {job_id}.pdf. Cleanup/retention must be tested against selected storage and persisted metadata, not assumed because S3 UI controls exist.

## Providers: executable support versus metadata/examples

| Provider/surface | Main implementation and support tier | Acceptance dependency |
|---|---|---|
| Phaxio | Dedicated service send/get status/cancel and outbound callback; inbound handler; HMAC support. Cancel method is not exposed through API/UI/SDK/MCP | Fake provider contract + signed callback/artifact tests; then authorized account/public callback real delivery check |
| Sinch | Dedicated v3 multipart/direct file send and status; inbound handler Basic/HMAC options | Fake v3 upload/send/status and correct inbound document; authorized account/public callback end-to-end |
| SignalWire | Dedicated Compatibility API send/status; outbound callback; traits outbound-only | Callback correlation/signature/error/status tests; account/from number + publicly accessible PDF URL |
| SIP/Asterisk | AMI originate, TIFF conversion, fax result listener, internal inbound receive; compose dialplan/config source | Asterisk/AMI, Ghostscript, SIP trunk/T.38 and shared faxdata; test call/real receiving endpoint for transmission proof |
| FreeSWITCH | fs_cli originate_txfax subprocess and internal outbound-result endpoint; traits inbound false; docs label preview | fs_cli installed/available, external FreeSWITCH gateway/mod_spandsp setup. API Dockerfile installs Ghostscript, not fs_cli |
| Documo | Config/traits/settings/plugins metadata only; no dedicated service, no main /fax branch | Explicit repair task: selecting documo currently falls through to SIP unless an installed manifest happens to exist; retain advertised provider and verify send/status/error |
| HTTP manifests | Concrete parser/runtime for send_fax/get_status with json/form/multipart/none + auth schemes; FEATURE_V3_PLUGINS dispatch | Install/config credential transfer, allowed domains, actual multipart/file bytes/status URL and error handling must work |
| Manifest cancel | Parser accepts cancel_fax but runtime has no method, API/UI none | Advertised optional manifest capability, not executable support |
| Local/S3 storage | Concrete storage classes, independent of provider metadata | Restart-retention-download-delete checks; S3 credentials/backend endpoint are external runtime prerequisites |

config/provider_traits.json declares 6 fax provider IDs and seven canonical trait keys. config/plugin_registry.json has five fax providers plus local/s3, omits Documo. Main plugin discovery adds Documo and scans installed provider files. No config/providers/*/manifest.json is checked into main. api_plugins_list.md and api/app/api_plugins_list.md contain six JSON examples: faxplus, ringcentral, interfax, sfax, dropbox_fax, pamfax; later providers are descriptive paragraphs. README's '20 others' is not twenty installed adapters/manifests. Template examples often use noncanonical schemas/URL interpolation; they need executable validation rather than import-only success.

HTTP runtime issues directly relevant to promised contract: empty allowed_domains allows unrestricted host instead of rejecting missing allowlist; URL {{...}} templates are not rendered, only explicit {path} params substituted; core send/refresh initializes runtime with empty credentials and chooses legacy backend in _send_via_manifest; no cancellation method; parser ignores traits for runtime; installation merely requires an id; render_only validation does not actually render/send. These are repair/verification requirements, not implementation prescriptions.

Main plugins/base interface and manager are marked feature-gated scaffolding. Manager adapts Phaxio/Sinch only; SIP explicitly unimplemented there. Node MCP plugin registry is independent helper tooling; it is not a backend provider plugin loader. plugin-dev-kit contains base/validation/testing/utils scaffolding but no server runtime loader for arbitrary generated Python/Node code.

## SDK contracts

Node package sdks/node/package.json: faxbot 1.1.0, CommonJS, Node >=18, Axios/FormData. FaxbotClient(baseUrl='http://localhost:8080', apiKey=null): sendFax(to,filePath), getStatus(jobId), checkHealth(), plugins. sendFax accepts existing .pdf/.txt path only; multipart to/file, X-API-Key, returns raw /fax JSON. Python setup.py/readme counterpart exposes FaxbotClient(base_url,api_key), send_fax(to,file_path), get_status(job_id), check_health(), plugins, also path-only PDF/TXT.

Both plugin manager extensions expose list, get config, update config, install. install calls POST /plugins/install, which main does not implement. list return shape actually {items} from API differs Python's list annotation and disabled fallback []. Node initializes support asynchronously in constructor; immediate list may return [] before check completes. Python duplicate import/property definitions are visible but last definition wins. Neither SDK has inbound methods, cancel, typed jobs, or artifact downloads. README 'identical SDK surface' is roughly core send/status/health plus plugin manager naming, not broad API parity.

Existing workflows .github/workflows/sdk-node.yml and sdk-python.yml build/pack and optionally publish; they do not execute HTTP client behavior tests. Scripts/release_npm.sh and release_pypi.sh publish MCP plus SDK packages. Package/version/schema agreement and actual installed package identity are separate from repository tests; no publication was attempted here.

## MCP contracts

Node package faxbot-node-mcp 1.0.1; server self-identification says 2.0.0. Python package faxbot-mcp 1.0.0; health says 2.0.0. api/package.json is a separate faxbot-mcp 2.0.2 metadata/dependency package with main index.js absent and no scripts, so Makefile mcp-http/mcp-stdio commands referring api npm scripts do not work.

Node common tools: send_fax(to,filePath|fileUrl|fileContent+fileName,fileType=pdf|txt), get_fax_status(jobId), get_fax(id), list_inbound(limit,cursor), get_inbound_pdf(inboundId,asBase64=false), plus helper StatusPlugin tools. FilePath resolves relative cwd; original PDF/TXT bytes passed multipart; fileUrl fetch is independent of provider manifest allowlist. Output is MCP text containing job ID/status; get_fax returns extra JSON text.

Concrete current blocker: read-only `node --check node_mcp/src/tools/fax-tools.js` failed at line 250: SyntaxError Identifier '.default' has already been declared. There are two default exports. All Node transports import this module (directly or via stdio buildServer), so Node transport parse/startup must be an explicit repair task, not removed coverage.

| Transport | Code contract | Current gap to verify/repair |
|---|---|---|
| Node stdio | Standard MCP Server/StdioServerTransport, list/call tools, prompts, readResource custom faxbot:inbound/{id}/pdf | Logs startup to stdout (protocol noise); advertises tools/prompts but resources capability missing; duplicate export blocks startup |
| Node HTTP | /health; /mcp POST/GET/DELETE, sessions; MCP_HTTP_API_KEY as X-API-Key or Bearer; default port 3001 | Initialization/session semantics must be tested against installed SDK; main parse blocker |
| Node SSE | /health; /sse GET, /messages POST/DELETE; OAuth issuer/audience/JWKS required; port 3002 | Existing SSE constructor/handler usage must be verified with actual installed SDK; main parse blocker |
| Node WS | port 3004; JSON list_tools/call_tool protocol; query/header key, open without key in dev | Source explicitly says MCP-like dev baseline, not full MCP transport; preserve dev surface without advertising standard protocol |
| Python stdio | Five tools matching above; path/url/base64 send; FastMCP runner selection | Prefix heuristic for inbound IDs fails for actual UUID IDs; cursor/limit unsupported by backend |
| Python SSE | Five tools; /health public; mounted FastMCP SSE app; OAuth JWT auth on all other paths; port 3003 | Same ID/pagination mismatch; non-base64 PDF result is bare relative path requiring separate auth |
| Python HTTP | /health and FastMCP streamable app mounted /; two tools send_fax + get_fax_status; send only base64/fileName | Less capability/input support than other transports; no transport auth (explicit doc comment); package console entrypoint http_server:main absent |
| Embedded Python mounts | main flags ENABLE_MCP_SSE/HTTP; configured paths /mcp/sse and /mcp/http; SSE can use inner app when OAuth disabled | Silent startup import/mount failure allowed; mount health must be verified, not config flag alone |

Additional concrete mismatch: get_fax infers inbound via in_ prefix, but main generates uuid.uuid4().hex for inbound IDs, so real inbound lookup routes to outbound. list_inbound returns raw list fallback rather than intended items summary. Node get_inbound_pdf returns incomplete resource content, and only stdio buildServer has readResource handler; these need protocol validation. Node shared fax client captures API environment at import; dotenv.config in transport entrypoint executes later. Python pyproject console entrypoints for HTTP/SSE reference nonexistent main functions and README.md absent from python_mcp. No MCP automated suite/CI exists; test-stdio script is a send smoke helper, not general protocol coverage.

## Desktop: source on main and packaging on retained branches

Main has electron/main.js + preload.js, ElectronAPIClient + unused useElectron, plus historical app.asar bundles, but main package.json has no Electron dependencies/scripts/build config and electron/assets only README. Main Electron entry dev URL is localhost:5173, while Vite uses 3000; production loads file://dist/index.html, while Vite asset base is /admin/ui/. App always uses web same-origin client, so file-origin API requests do not target localhost correctly. Menu/tray emit route strings but no main App listener is wired.

ElectronAPIClient source calls stale paths /admin/reload-settings, /admin/jobs, /admin/diagnostics; lacks the complete AdminAPIClient method set and maps shapes inconsistently. Explicit repair tasks must retain send/jobs/inbox/settings/diagnostics/keys/plugins/MCP/logs/tools parity, native menu/tray navigation and native file dialog behavior.

Branch packaging provides electron 28, electron-builder 24, concurrently/wait-on/cross-env; app entry main; icon.icns/icon.ico/icon.png/tray-icon.png. macos branch builds DMG x64/arm64; Windows NSIS x64/ia32 + portable x64; Linux AppImage/deb/rpm/tar.gz x64. Branch App wires ElectronAPIClient and handles menu routes; Vite switches relative base when ELECTRON env set and port 5173. Need reconcile best branch integration into preserved visual baseline. Build scripts do not themselves set ELECTRON=true, so packaging file asset behavior remains an acceptance task.

Docs claim auto-updates/native notifications/code signing. Source has no electron-updater dependency or autoUpdater/notification integration; signing depends external certificates; existing app.asar files do not prove fresh packaging or compatibility. Retain promises as explicit completion/verification tasks; do not infer them from old binaries.

## iOS recovered contract and claim boundary

origin/iOS: ios/FaxbotApp has Xcode project, XcodeGen project.yml, SwiftUI sources, resources, FaxbotShare, Fastlane, scripts/ios/{build,archive,run-sim}. iOS deployment target 26, app net.faxbot.ios, extension net.faxbot.ios.share. Tabs Send/Inbox/History/Settings. Client REST paths are /health, /fax multipart to/file, /fax/{id}, /inbound, /inbound/{id}/pdf. FaxJob Decodable tolerates optional backend/pages/error/timestamps; inbound type expects flat array as main provides.

Source Send flow has typed comma-separated recipients, sequential one-job-per-recipient sends, Scan Document -> VisionKit images -> US Letter PDF, Type Text -> note.txt, custom locally stored contacts/recent chips. History persists locally, status polls every 3s, retries through document picker/scan/text; Inbox lists/downloads/previews PDFs and local notifications on newly seen IDs. Notification preferences and Haptics exist. Share extension accepts first PDF/image attachment and posts multipart /fax, but does not check response before completing. API returns '' jobId when accepted JSON lacks id; client-side limit hardcoded 10MB.

Pairing calls POST /mobile/pair with {code,device_name}, expects {base_urls:{local,tunnel,public},token}, then stores config in Keychain + App Group defaults. No /mobile/pair route exists in main, development, traits branch, or recovered iOS backend. Main /admin/tunnel/pair only saves expiry in memory. This is concrete server/mobile completion work. isOnLAN always true, so local preference does not implement claimed off-LAN fallback.

Main apps/ios.md advertises photo picker, combined photos/scans, pre-send confirmation dialog, device contacts filtering labels containing fax, icons, and source paths. Recovered branch has scanner + local contacts picker, no primary Send photo picker/device Contacts framework/pre-send attachment confirmation, and PDF upload only via history resend/share path. Treat these as advertised completion tasks, not demonstrated features. Images in Share extension are actual code. Build scheme/extension dependency/resource/privacy/entitlement validation is still required; XcodeGen comments disable entitlements for Personal Team despite README saying App Group enabled, and extension source references PDFComposer defined under app-only source directory.

scripts/ios/build.sh pipes xcodebuild through xcpretty with `|| true`, so an exit success is not build evidence. Acceptance must use actual xcodebuild result logs. Simulator can verify most UI/network behavior, physical device needed for camera/scanner/device notifications/share-sheet realism. External dependencies: installed Xcode 26-compatible runtime, XcodeGen, signing/team/App Group provisioning for device/release, Apple account/upload eligibility, actual TestFlight build/source identity, connected phone + reachable server/tunnel. Repository source reconciliation is internal work; these runtime/distribution dependencies must be recorded distinctly.

## Branch-only retained surfaces to reconcile

origin/development has /admin/providers, /admin/config/effective, /admin/config/import-env, POST /admin/providers/{id}/test, inbound purge-by-sid, GET /fax/{id}?refresh, unified /webhooks/inbound, /admin/tunnel/register-sinch, cloudflared log view/watcher, WireGuard import/download/delete/QR. Web UI adds ProviderSetupWizard, InboundWebhookTester, OutboundSmokeTests, Tunnels Tools tab and useTraits. These are code-present, not main-present or validated in this pass.

Traits-canonical branch further contains ConfigurationManager, PluginMarketplace, ProviderHealthStatus and EventStream components; App mounts config and marketplace and expanded diagnostic/provider testing. Backend adds config v4 hierarchy/effective/safe-keys/cache controls, identity/session auth, /metrics, events recent/SSE/types, providers admin routes, marketplace, webhook processing/DLQ/services/storage shims. Compared main it is ~12k inserted lines across 93 selected files. Existence and comments do not prove completion. Preserve intended surfaces by explicit scope reconciliation, do not adopt a whole branch as evidence of a finished implementation. Primary architecture owner should decide integration boundary with feature contracts recorded.

## Documentation source-of-truth contract: Docs Autopilot + Mike + code generation

Existing main automation:

- .github/workflows/docs-ai.yml named Docs Autopilot (LLM): pushes to development touching api/config/sdks/UI generate heuristic plan; manual apply=true invokes OpenAI/Anthropic, applies patch onto mkdocs and proposes PR. Script scripts/docs_ai/generate_docs_from_diff.py reads changed filenames + excerpts of AGENTS.md/OpenAPI file/.env.example/provider_traits; does not inspect actual changed source diff or current docs content; OpenAPI file is not generated there. It defaults base origin/development, so development push diff base..HEAD likely empty after checkout/fetch. No main trigger. Apply failures/empty commits are swallowed with echo. No strict docs validation before PR.
- .github/workflows/api-docs.yml: development API changes/manual => import app and app.openapi() in disabled/test mode, overwrite metadata version=1.0.0 and production server URL, Redocly build, clone separate faxbot.net repository via deploy key, copy /api and commit/push main. Uses FAXBOT_NET_DEPLOY_KEY; API_DOCS_SETUP.md calls it FAXBOT_NET_DEPLOY_TOKEN. This is actual code-derived API generation but hardcoded version/servers and split branch/repo identity need reconciliation. Swagger link promoted by autopilot is not created by this workflow.
- .github/workflows/mkdocs-deploy.yml: mkdocs branch/manual => mkdocs build --strict, Mike version short docs branch SHA, deploy gh-pages alias latest/default latest, root CNAME docs.faxbot.net. Plugins: Material, search, mike, redirects, exclude; tools unpinned. mkdocs.yml edit_uri points edit/mkdocs/docs. Docs SHA currently differs from code main identity.
- scripts/docs_tools contains mirror_from_branch, cleanup, compare_docs_trees; migration tooling is not automatic current-code documentation.

User requested contract: documentation derives from the accepted main commit, preserves Docs Autopilot + Mike (+ OpenAPI/Redocly/Material/redirects), and generations/deployment identify their source main SHA. Main changes must trigger generation/validation, docs must cover actual API/SDK/MCP/provider/UI/mobile/desktop behavior, and published latest/version links must correspond to that same accepted main release. Generation failures, empty patches, stale schemas, broken links, or copied old branch snapshots must fail visibly. Completion requires source->CI artifact->published API/docs page identity and real browser routes, beyond a strict local build. This is an acceptance requirement; no implementation design prescribed here.

## Existing tests/build scripts and their limits

- API pytest files: test_api, test_api_keys, test_api_scopes, test_rate_limit, test_phaxio, test_freeswitch, test_admin_hybrid, test_inbound_gating, test_inbound_internal. Parent reports 27 baseline passes in disabled mode; not rerun by inventory agent. conftest sets FAXBOT_TEST_MODE=true. FAX_DISABLED, fake service mocks and placeholder conversion avoid actual transmission/file fidelity.
- .github/workflows/ci.yml installs Python 3.11 + Ghostscript, pytest FAX_DISABLED=true. No UI build/browser, Node MCP, Python MCP, SDK HTTP, desktop, iOS, migrations, container persistence, docs smoke, or artifact fidelity acceptance in this CI.
- UI package scripts dev/build (`tsc && vite build`)/preview/test (Vitest); no test files found under UI. API Dockerfile builds UI via npm ci, copies config/UI, installs Python MCP + Ghostscript, serves Uvicorn. Package locks present but excluded from general listing intentionally.
- Makefile test is docker compose run api pytest; Alembic helpers + inbound smoke/e2e. MCP Makefile api npm targets refer missing scripts.
- scripts/smoke-auth.sh calls pytest; inbound-internal-smoke uploads synthetic artifact, e2e-inbound-sip exercises Asterisk/shared volume; node_mcp/scripts/test-stdio.js is file/send smoke. None alone proves physical send, provider callback receive, app distribution, or published docs.
- sdk build workflows pack/twine only; release scripts actively publish and were not run.

## Practical acceptance matrix for complete handoff

All rows should carry accepted source SHA, test result/artifact identity, observed result, and prerequisite status. Use synthetic/non-sensitive fixtures. A mock/disabled check must be recorded separately from live provider/device/distribution proof.

| Acceptance area | Required observable check | Dependencies / distinction |
|---|---|---|
| Preserve liked web UI | Desktop+mobile widths, light/dark, all original tabs/subtabs, logo assets, drawers, tooltips/copy/download, no blank/error paths; compare baseline images | Current UI visual baseline; real browser, production built assets, MSW disabled |
| Login/navigation | Invalid/valid bootstrap+scoped admin login, remembered login/logout, Dashboard links correct, refresh preserves usable state | Admin key lifecycle; local access rule |
| Send/document fidelity | Real UTF-8 TXT and multi-page PDF; filename containing test; outgoing/downloaded PDF renders actual content; safe number/type/size errors | Ghostscript real conversion; FAX_DISABLED does not count; fixture hash/pages/visual inspection |
| Jobs lifecycle | Accepted job persists across API restart; progresses queued/in_progress/success/failure; callback/status refresh agrees; correct provider chosen in hybrid mode | Fake provider server deterministic checks; authorized live fax delivery separate |
| Inbound durability | Valid signed/internal receive + retry replay creates one row/artifact; failed download retried successfully; actual PDF renders; restart list/download persists | Synthetic provider ingress/storage; account callback proof separate |
| Auth/security errors | Scope matrix fax:send/fax:read/inbound:list/inbound:read/keys:manage; create/rotate/revoke/expiry; wrong/expired file token; 429 with Retry-After | Multiple clients, no credentials/documents/numbers leaked in logs/console |
| Effective settings | Change all visible fields/provider selections/storage/flags/limits; GET reflects effect; reload/app restart agree; exported persisted config survives restart | Masked field unchanged semantics; branch config reconciliation |
| Provider traits | /admin/providers/config accurately advertises active outbound/inbound and per-provider actions; unsupported combinations yield clear UI results | main lacks traits API; retained development code |
| Provider suite | Phaxio, Sinch, SignalWire, SIP, FreeSWITCH, Documo send/status/errors; inbound only for supported traits | Fake provider tests + external provider/trunk/runtime acceptance; Documo explicit repair |
| Plugins/manifests | List/configure/select, install/bulk import/dry-run, credentials applied, URL/body bytes/normalized status, blocked domains/errors, restart persistence | FEATURE_V3_PLUGINS; generic built fake provider; no parse-only success claim |
| Plugin builder | Accessible UI; downloaded Python/Node code parses and conforms to advertised dev-kit contract, clearly distinguishes scaffold from transmitting adapter | Existing unmounted component; advertised user action retained |
| Storage/retention | local + S3 store/list/read/download/expiry/delete/restart; correct artifact refs; outbound download works chosen storage | Local fixtures, fake S3 or scoped test bucket; external storage proof recorded |
| Diagnostics/logs/tools | Current provider checks true/failed appropriately, test job correlated, filtering/tail/download/restart affordances truthful; terminal interactive/reconnect/resize | Runtime dependencies and admin restrictions; no test placeholder proof |
| Node MCP | Syntax/startup; initialize/list/call tools/read resource; STDIO pure protocol; HTTP session lifecycle+auth; SSE connect/post/close+JWT; WS custom protocol | Node installed package + SDK compatibility, local fake JWKS, repaired duplicate export |
| Python MCP | Installed package entrypoints; five-tool parity/path/url/base64 inputs and inbound UUID reads; HTTP promised capability/auth; mounted health | Python MCP runtime/lifespan; no repo-import-only proof |
| SDKs | Pack/install both SDK artifacts; send/status/health/plugin config against same accepted API, auth/errors/flat-list response agreement | Published registry version/digest if release required; installPlugin missing-route repair |
| Electron | Fresh macOS/Windows/Linux package build; launches/connects; menu/tray/file dialog; every web flow; assets/file URLs; advertised update/notification behavior | Recovered branch deps/icons/integration, OS runtime, signing/update channel external |
| iOS | Reconcile source; actual build result app+share; connect/manual config/pair/QR; send TXT/PDF/scan/image with confirmation; contacts/multiple recipients/history/inbox/status/notifications/share | iOS 26 Simulator + device camera/share, signing App Group, pair server, reachable tunnel; TestFlight identity separate |
| Tunnel/pairing | Tailscale/WG/Cloudflare controls reflect running state; start/stop/restart; WG import/download/QR; mobile pair expires/redeems once/issues scoped token; off-LAN reaches server | External host tools/tunnel credentials; main in-memory labels do not prove connectivity |
| Docs automation | Main commit triggers Autopilot/OpenAPI generation and strict/link/schema checks; same main SHA in build artifact/Mike published latest/API docs; no stale advertised claims | Separate faxbot.net/gh-pages access, CI secrets toolchain. Live page/browser proof after deploy |
| Delivery reconciliation | Accepted main source, green required CI, deploy/container identity, SDK/MCP packages, desktop/mobile artifacts, docs latest correspond | Handoff no known unfinished tasks: explicit outstanding dependencies cannot be reported complete |

A complete proof package should pair each claim with the relevant tier: source behavior, deterministic/fake-provider checks, build/package install, real browser/app behavior, live provider callback/transmission, device behavior, and published release/docs identity. This inventory only supplies source evidence plus the Node syntax failure.

## Acceptance priority and retained source pointers

Priority orders work and evidence; every retained/advertised surface still requires closure for the requested no-unfinished-work handoff.

| Priority | Contract blocker / acceptance | Source pointer |
|---|---|---|
| P0 | Preserve current console and fix dead Dashboard destinations; accepted send/inbound must use real artifacts and survive restart | main api/admin_ui/src/{App.tsx,components/Dashboard.tsx,api/client.ts}; api/app/main.py |
| P0 | Repair Node parse/startup and protocol handshake before claiming MCP support | main node_mcp/src/tools/fax-tools.js:250; src/servers/{stdio,http,sse,ws}.js |
| P0 | Restore Documo's advertised executable provider behavior; eliminate wrong-provider fallback/config mismatch | main Settings.tsx/SetupWizard.tsx; main.py send_fax dispatch; config/provider_traits.json |
| P0 | Reconcile provider traits/effective config so every visible selection resolves to actual send/receive/storage behavior | origin/development api/app/main.py /admin/providers,/admin/config/effective and UI hooks/useTraits.ts |
| P0 | Connect iOS pairing request to working backend contract and verify manual send/status/inbound | origin/iOS ios/FaxbotApp/Sources/Faxbot/APIClient.swift + SettingsView.swift; POST /mobile/pair absent all inspected backend refs |
| P0 | Main-commit code-derived docs, generation failure visibility, source SHA agreement with deployed Mike latest/API docs | main .github/workflows/{docs-ai,api-docs,mkdocs-deploy}.yml; scripts/docs_ai/generate_docs_from_diff.py |
| P1 | Repair complete desktop packaging/client/menu integration using retained sources while preserving UI | origin/electron_{macos,windows,linux}: api/admin_ui/{package.json,vite.config.ts,src/App.tsx,electron/assets}; main src/api/electron-client.ts |
| P1 | Complete promised SDK/MCP surfaces and installed-artifact compatibility, including real inbound IDs and plugin install | main sdks/{node,python}, python_mcp, node_mcp; API /plugins/install absent |
| P1 | Complete executable manifests/builder/config/runtime apply; verify existing native/storage ingress and every provider | main api/app/plugins/http_provider.py; components/{Plugins,PluginBuilder,PluginConfigDialog}; provider services/storage.py |
| P1 | Complete tunnel controls + recovered WG/Sinch webhook helpers; prove running status and off-LAN connectivity | origin/development api/app/main.py /admin/tunnel/*; components/TunnelSettings.tsx; main in-memory tunnel endpoints |
| P1 | iOS main advertised photo/PDF/confirmation/device contacts flows and share-extension build/behavior; complete signing/distribution proof | origin/iOS ios/FaxbotApp/{project.yml,Sources/Faxbot/ContentView.swift,Sources/FaxbotShare/ShareViewController.swift}; main apps/ios.md |
| P1 | Reconcile added provider setup/webhook tests/smoke/config/marketplace/events surfaces as retained product scope; verify rather than assume branch completion | origin/development components/{ProviderSetupWizard,InboundWebhookTester,OutboundSmokeTests}; origin/feat/p5-pr0-traits-canonical components/{ConfigurationManager,PluginMarketplace,EventStream,ProviderHealthStatus}, routers/* |
| P1 | Complete diagnostics, logs, allowlisted actions, terminal interaction and privacy checks, build/CI/package/deploy proof | main components/{Diagnostics,Logs,Terminal,ScriptsTests}; app/{main,terminal,audit}.py; .github/workflows/ci.yml |

## RBAC follow-up: existing intent and unfinished boundaries

Updated user steering: RBAC must be finished; UI upgrades are permitted while retaining the liked console and all features. The existing TestFlight app is fine-ish and compatibility must be preserved; recovered origin/iOS is historical source, not proof of the current distributed binary. This supersedes any interpretation that the UI is frozen or that replacing mobile behavior is requested.

Narrow source inspected: origin/auto-tunnel at 3905a8cb080d83578b393c9065e37a82ec66c97d, api/app/security/{auth_sessions,user_traits,permissions,session_tokens}.py, identity_models.py, plugins/identity/{base,shims/sqlalchemy/plugin,providers/ldap/plugin,providers/saml/plugin,sqlalchemy/manifest.json}, routers/admin_users.py, relevant auth/UserManagement/App/client/CSRF source. No source modifications or credentials/network access.

### Actual existing role/permission contract

- Only named role is `role.admin`, derived from API-key `*` or `keys:manage` scopes. No role CRUD, persisted role definitions, role assignments, role membership table, persisted groups, tenant membership, or resource ownership ACL exists in these files. Bootstrap identity create call passes `{role: admin}` in startup, but SQLAlchemy create_user does not persist traits. There are sender/read-only-style scope concepts, not defined operator/viewer role objects.
- Legacy scopes remain actual server authorization: fax:send, fax:read, inbound:list, inbound:read, keys:manage, wildcard. require_admin accepts environment bootstrap key/keys:manage DB key (plus branch developer bypass); require_api_key checks API-key headers and does not inspect session cookie. Thus username/password login cannot authorize protected API routes by itself.
- Canonical permission grammar is `{namespace}.{resource}:{action}` with actual examples admin.console:access, fax.jobs:send, fax.jobs:read, fax.inbound:read. Mapping: wildcard grants wildcard+common permissions; keys:manage -> admin.console:access; fax:send -> fax.jobs:send; fax:read -> fax.jobs:read AND fax.inbound:read. Reverse mapping inbound permission -> fax:read. inbound:list/read scopes are not mapped at all. Only route using require_permissions is demo GET /admin/permissions/check; real routes remain legacy guarded.
- UI trait mapping: admin gets ui.terminal/logs/diagnostics/plugins/settings/send/jobs/inbound; fax:read gets ui.jobs AND ui.inbound; fax:send gets ui.send. inbound-only scopes get no UI traits. keys:manage implies full UI including send/inbound even without corresponding legacy scopes. `/admin/user/traits` is admin-only and reports API key ID as user ID. Non-admin users cannot fetch their own traits or get past API-key login's admin getConfig call, so read-only/send trait paths are unreachable as ordinary user workflows.
- UI branch adds Users sub-tab and UserManagement. Exact API: GET /admin/users -> {users:[id,username,display_name,email,is_active,created_at]}; POST {username,password,display_name,email}; PATCH /{id} only display_name,email,is_active. No roles, scopes, password reset/change, delete user, own profile, sessions list/revoke, assignment, or per-resource grant controls. Settings/Keys/Plugins accept readOnly; Send/Terminal/Scripts gate traits. Users control is not individually trait-gated. UI has only API-key login; session/CSRF flags are status chips, no password-login UI/refresh/logout integration.

### Identity and session behavior actually implemented

- DBUser (`id_users`) has username unique, display/email, salted+peppered PBKDF2-SHA256 password hash (200,000 rounds), traits_json and timestamps. Disabled state is stored as traits_json.is_active. SQLAlchemy authenticate_password checks username/password but ignores is_active; find/get user return empty traits; no group/role loading. Disabling user therefore does not block new password auth and does not revoke sessions.
- DBUserSession (`id_sessions`) has id,user_id,token_hash,created_at,expires_at and token_hash uniqueness, but session functions do not use it. Identity base models User(groups,traits), Group(name,traits), Session(user_id,times,traits), AuthResult establish intended extension points only.
- Sessions are in-memory maps, not persistent/shared between workers. `create_session` returns raw sid/token; only HMAC-SHA256 peppered token hashes are indexed. Minimum TTL 60s; default 3600; login accepts unbounded ttl_seconds. validate expiry/revoke/rotate exist; rotation removes old token. Plugin create_session discards raw token, returns zero timestamps; plugin revoke_session(session_id) is a no-op. No all-user session revoke.
- `/auth/login`, `/auth/logout`, `/auth/refresh` only mounted with FAXBOT_SESSIONS_ENABLED. Session flag requires CONFIG_MASTER_KEY (44-char text check) + FAXBOT_SESSION_PEPPER. Login tries active identity plugin, falls back to username admin + FAXBOT_BOOTSTRAP_PASSWORD. AuthResult's DB identity is discarded and session user_id is supplied username. Cookie fb_sess HttpOnly, SameSite strict, Secure conditional ENFORCE_PUBLIC_HTTPS; no persistent max-age. Logout revokes that token/clears cookies, refresh rotates. There is no /auth/me/current-user endpoint or permission hydration from session.
- Optional CSRF sets readable fb_csrf at successful login and middleware compares x-csrf-token. Middleware applies to every POST/PUT/PATCH/DELETE, including login, API-key clients, provider/internal webhooks; it neither restricts checks to authenticated cookie-session requests nor exempts initial login. When enabled, first login has no CSRF cookie/header and external callbacks/API-key mutations fail. UI client does not send CSRF header.
- SQLAlchemy manifest advertises user_store/session_store/csrf_supported, but these are metadata rather than complete runtime behavior. LDAP and SAML source explicitly return not_implemented and are unwired skeletons. These should not be claimed working SSO/RBAC integrations.

### P0 RBAC acceptance needed for a finished product

| Requirement | Concrete check against finished contract |
|---|---|
| Defined roles/resources/actions | Publish the chosen role-permission matrix and resource scope; existing source only establishes admin + API scope concepts. Verify every role/action allowed and denied on API, not UI visibility alone |
| User lifecycle/assignment | Admin creates a user, assigns/changes intended role(s), user signs in and gets own capabilities; edit/disable has immediate authorization effect; uniqueness/errors do not expose password hashes |
| Working authentication | Password session and API key are distinct credentials resolving actual identity+permissions. Username/password user can use intended console without bootstrap key. Current mobile/SDK/MCP X-API-Key contracts keep working |
| Session lifecycle | Expiry, bounded TTL, token rotation invalidates old token, logout, disabled user/all-session revoke, multi-worker/restart policy behave as documented; DB/identity consistency and cookie flags are verified |
| Least privilege | Sender/read-only/inbound-only/admin scenarios use real route tests; admin-only users/settings/keys/plugins/tunnels/actions/terminal correctly deny lower privilege; inbound/list/read mapping agrees with UI traits |
| Resource access | Jobs/artifacts/inbox metadata+PDF endpoints enforce the chosen own/group/global scope; direct IDs/token downloads cannot bypass it; no ownership policy exists in inspected source |
| CSRF/protocol compatibility | Cookie-session mutation rejects missing/mismatched CSRF and accepts valid; initial login succeeds; API keys/SDKs/MCP/iOS/provider/internal callbacks work under documented credential semantics |
| UI flow | Password login/logout/current-user/role-aware controls/User Management assignments are usable and errors honest; source API-key login and Users Create/Disable table alone do not qualify |
| Audit/secret handling | Role/permission changes, user disable, login/logout/session revoke and authorization denial audited with identifiers; no password/token/raw document/recipient data leaks |
| Documentation/live evidence | RBAC docs derive from accepted main contract; CI role/resource/session negative tests and real browser sign-in + forbidden actions verify the published identity |

This source does not specify named operator/viewer roles or per-tenant resource policy. The architecture owner must make the completed role/resource contract concrete; inventing such names and calling them existing intent would be inaccurate. LDAP/SAML completion is separate from local RBAC unless explicitly included in product scope. The fixed handoff cannot claim RBAC complete with these session/UI stubs unchanged.
