# Faxbot agent guide

Faxbot is one self-hosted fax product: a FastAPI backend, React Admin Console, provider adapters, SIP fax engines, SDKs, and MCP servers. The current work makes delivery cheaper and more reliable while keeping the sender's normal fax-number workflow.

The future enterprise direction adds accountable correspondence: reusable intake, ownership, deadlines, approvals, recipient policy, evidence and retention, with optional templates and guided administration. This is documented future work; preserve the current Phase 1 and four-fix implementation scope.

## Who reads what Faxbot writes

- At a company running Faxbot, staff never open the console or the docs; they get their faxes by email. Everyone who reads the console, the `faxbot` command line and the docs is the administrator who set Faxbot up and handles its exceptions, usually the same person who runs its server, network and provider accounts.
- Speak to that person directly and tell them what to set and where ("In your phone system, send fax calls to 192.168.1.20, port 5060"). Never write "ask your administrator", "give this to your administrator", "ask whoever installed Faxbot" or anything else that treats the reader as someone without access. The reader is that person.
- Name another party only when real companies really do have one: the fax carrier or provider, a partner who manages an Avaya or BT phone system, the recipient's fax machine. Even then, give the reader the exact settings to check or pass on.
- Keep developer material (API internals, revision IDs, plugin manifests, environment variables) in developer reference pages, out of operator guides and screens.

## Start here

- [Planning sources](planning/README.md) are maintained outside the generated documentation tree. Keep enterprise requirements in `planning/enterprise-correspondence.md`; the old `docs/architecture/` page is a pointer. Do not target planning with generated patches.
- Read [README.md](README.md) for current capabilities and its [roadmap](README.md#roadmap) for planned work. Verify against the current checkout before treating a capability as complete.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks. Preserve unrelated changes and coordinate when another agent owns the same files.
- Historical Markdown from 2025 is kept locally under `.archived/`, preserving its original paths. That folder is excluded from Git and Docker builds. Use current docs for instructions; do not restore archived pages to navigation or treat their old plans as active work.
- The cost-reduction work now includes routing and billing observations, an intake queue with SMTP delivery, verified direct partners, and recipient-approved case packets. The next focused batch covers number normalization, TXT pagination, client idempotency, and one-bit PDF images.
- Read the [enterprise architecture](planning/enterprise-correspondence.md) for the current-code status matrix, proposed interfaces, E0–E4 dependencies, acceptance criteria and deferred integration questions. The smallest future foundation is trustworthy acquisition plus one generic import, owned queue, acknowledgement target and evidence export.
- When present locally, `MAJOR-IMPROVEMENT-AGENT-PROMPT.md` contains the research implementation brief and the four-fix follow-up. `MAJOR-IMPROVEMENT.md` and `research/faxbot-cost-research-2026-10-03/` contain supporting research. Start with the package's `README.md`, `context/IMPLEMENTATION_HANDOFF.md`, `context/CLAIMS_AND_UNCERTAINTIES.md`, and `ADDENDUM_2026-10-03.md`. These local research files are intentionally excluded from Git; do not commit them. Their dated code snapshots and conversation instructions are context, not proof of current behavior or authorization for unrelated work.

## Important code and documentation

| Area | Code | Read first |
| --- | --- | --- |
| Outbound delivery and provider identity | `api/app/outbound_store.py`, `outbound_worker.py`, `outbound_transport.py`, `provider_execution.py` | [Durable outbound design](docs/architecture/2026-10-02-faxbot-durable-outbound.md) |
| Route selection, cost estimates, and charge reconciliation | `api/app/routing/` | [Delivery routes and case packets](docs/operations/delivery-routes.md) |
| Received-fax notifications, document fetching and provenance | `api/app/inbound/` | [Receiving faxes](docs/operations/receiving.md) |
| Intake and email delivery | `api/app/intake/` | [Intake](docs/operations/intake.md) |
| Work queue, acknowledgement targets, evidence export and generic import | `api/app/work/` | [Work](docs/operations/work.md) |
| Encrypted delivery and peer verification | `api/app/direct/` | [Direct delivery](docs/operations/direct-delivery.md) |
| Accepted case documents and packet preparation | `api/app/cases/` | [Case packets](docs/operations/delivery-routes.md#case-packets) |
| Document conversion and request identity | `api/app/conversion.py`, `api/app/request_identity.py` | [Conversion implementation](api/app/conversion.py), [held test jobs](docs/setup/test-mode.md) |
| Permissions and saved configuration | `api/app/access/`, `config_values.py`, `config_store.py`, `config_activation.py` | [Access control](docs/security/access-control.md), [configuration design](docs/architecture/2026-10-02-faxbot-configuration-activation.md) |
| Console and clients | `api/admin_ui/`, `sdks/`, `node_mcp/`, `python_mcp/` | [Console](docs/admin-console.md), [SDKs](docs/sdks/index.md), [MCP](docs/mcp/index.md) |
| Schema and provider capabilities | `api/app/schema*.py`, `api/alembic/versions/`, `config/provider_traits.json` | [Schema design](docs/architecture/2026-10-02-faxbot-schema-foundation.md) |
| Future enterprise workflows, templates and setup | Extend existing access, intake, delivery and configuration boundaries; proposed modules are not current APIs | [Enterprise architecture and acceptance criteria](planning/enterprise-correspondence.md) |

## Enterprise planning boundaries

- **Enterprise testing is synthetic/local only for development and CI.** Unit tests, mocked contracts and local integration tests are the acceptance criteria. Missing live customer systems, enterprise credentials or real end-to-end validation must never block CI, merge, release or completion of the generic capability. Record external validation separately as not performed. See the [testing boundary](CONTRIBUTING.md#enterprise-testing-boundary); it takes precedence over broader live-verification language in older plans.
- Keep product defaults, documentation and core models company-neutral. Do not assume a customer's provider, telephony stack, legal duties or case system. Record integration questions for the selected future pilot.
- Preserve independent inbound and outbound providers. The supported trust model remains a dedicated installation per organization; policy scopes do not establish multi-tenant isolation.
- Keep document acquisition, transport success, owner acknowledgement, internal approval and business completion distinct. Existing SMTP acceptance and case-ledger fax success cannot stand in for human or external-system acknowledgement.
- Put industry forms, fields, terminology and rules in optional templates/integrations. Templates need authoritative sources, applicability, immutable versions, required settings, evidence requirements and explicit coverage/manual responsibilities. UK/AU/US healthcare is a research priority, not an already validated compliance mode.
- Resolve settings at organization, mailbox and workflow scope with per-setting inheritance rules and provenance. Unknown or conflicting required settings remain visible and block the affected operation; setup must not invent legal defaults or ask staff to reinterpret regulations on every send.
- Required identity, approval, retention/hold and processing controls precede real workflows that depend on them. A synthetic foundation pilot need not implement every integration. Research and this architecture authorize no expansion of an unrelated implementation batch.

## Keep documentation current as capabilities land

- Adding, changing, or removing a capability includes updating `README.md` and its bottom-of-file roadmap immediately, in the same change as the implementation. Do not defer this to a later release or documentation pass.
- Describe the behavior users can actually use, including material setup requirements and limits. Move a roadmap item to implemented only when its usable implementation and relevant checks are complete. Keep partial work and experiments explicitly unfinished.
- Guides in `docs/` (setup, how-to, behavior and limits) are written by Docs Autopilot from the merged code, not by the agent that wrote the code: it sees only the code and the docs, so it reports what the code does and catches contradictions. After merging a wave, run the Docs Autopilot workflow on the integration branch (`apply`, base = the commit before the wave) and review its pull request like any other. Builders keep generated references current (`make cli-docs`, `scripts/docs_ai/generate_reference.py`), add new pages to `mkdocs.yml` when a feature needs one, and update this guide when the project direction or important entry points change.
- Keep the roadmap in the README as the shared status source; do not create a competing roadmap. Research ideas, synthetic benchmark results, and advertised prices must not become claims of shipped behavior or measured financial savings.
- Before reporting completion, check that capability descriptions, roadmap status, examples, and links agree with the code. Summarize relevant validation and any remaining limitations.
- Write documentation (MkDocs pages, the README, `planning/` and internal notes) in clear, natural prose. Take ASD-STE100 Simplified Technical English as loose inspiration only (about 20%): prefer shorter sentences and active voice where they help, and use one term for one thing. Never chop explanations into clipped fragments; read it back as a person would. Keep technical precision, and say plainly what is unverified or uncertain.

## Engineering boundaries

- Preserve the public fax API, authorization checks, immutable attempt/provider bindings, and original document content. Make schema changes through additive migrations following the existing frozen-schema pattern.
- Never blindly retransmit after an uncertain provider or direct-delivery outcome. Client idempotency and transport reconciliation are different responsibilities; retain both.
- Unknown cost is not zero cost. Keep estimated, provider-reported, and settlement observations separate, and apply billing increments per attempt.
- Direct delivery requires verified peers; case-document reuse requires recipient approval. Keep capability and fallback limits visible in the relevant documentation.
- Use plain language in the product and operator docs. Keep implementation details in developer documentation, and use provider capabilities rather than scattered backend-name checks.
- Use synthetic documents and mocked/local providers for automated checks. Keep credentials, document contents, and personal data out of committed fixtures and reports. Run checks appropriate to the change using the contributor guide; document-only edits need link/content validation rather than fax transmissions.
