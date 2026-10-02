# Full refresh task graph

This is the whole-product sequence; the detailed first implementation plan is `../superpowers/plans/2026-10-02-faxbot-document-integrity.md`. Later subsystems receive concrete briefs before dispatch. These are intermediate working slices; the release is not finished until every required slice and release gate passes.

| Work | Depends on | Verifiable result | Execution owner |
|---|---|---|---|
| Product/branch reconciliation | Inventory (done) | Retained capability matrix and exact branch sources | Primary |
| Document integrity (complete, 6a0afbba) | Reviewed first plan | Actual contents survive upload/conversion; honest failure | Sol implementer + independent review |
| API/MCP runtime and container foundation (complete, c17e10c9) | Document integrity | Coherent dependencies; pip check and real image startup; supported lifecycle | Bounded Sol implementation + primary container proof |
| Versioned schema and configuration foundation | Product reconciliation + runtime foundation | Fresh install and upgrade; no swallowed migrations; consistent provider identity | Primary design, bounded Sol implementation |
| Durable outbound delivery | Document + schema/config | Restart recovery, guarded transitions, no blind ambiguous resend | Primary architecture/integration |
| Durable inbound receipt | Document + schema/config | Retryable acquisition/storage; deduplication; honest receipt states | Primary design, bounded Sol implementation |
| Complete identity and RBAC enforcement | Schema/config | Users/groups/roles/resource grants/sessions/keys enforced for direct calls | Primary architecture + Sol bounded tasks |
| Complete RBAC UI | RBAC enforcement | Admin manages assignments; ordinary users can perform only permitted workflows | Sol + browser verification |
| Complete provider adapters and traits | Delivery + receipt + configuration | Every retained provider follows verified capabilities and common lifecycle | Sol bounded adapter tasks + primary reconciliation |
| SDK and MCP compatibility | Delivery + receipt + RBAC | Supported clients/transports initialize and execute against real backend | Sol |
| iOS and desktop compatibility | Backend contracts + RBAC | Existing app workflows/builds function; required pairing integrated | Sol bounded tasks + primary release context |
| Functional networking and pairing | Configuration + RBAC | Actual tunnel/URL/pairing behavior; permissions and expiry enforced | Primary + bounded Sol tasks |
| Code-derived documentation automation | Stable backend schema/traits | Main change → validated generation → strict build → Mike/API publication | Sol + primary external verification |
| Deployment and maintenance | Schema + complete backend/clients | Same tested container artifact, backups/restoration, health, upgrades | Sol + primary integration |
| Full security/dependency/license review | Complete implementation | Findings resolved; supported dependency and license inventory | Independent Sol review + primary adjudication |
| Release and recipient verification | Every required prior gate | Real controlled send/receive; exact CI/deploy/docs identity; repeatable handoff | Primary |

## Scope refinements from the user

- RBAC must be complete, not only legacy key scopes or UI gating.
- UI upgrades are permitted; retain its familiar strengths.
- Existing TestFlight iOS app is fine-ish; preserve compatibility and repair necessary workflow issues.
- Docs are generated from code through Docs Autopilot, Mike and associated tooling and must update on every main commit.
- No known required work is handed to the recipient unfinished.

## External prerequisites to establish early

Actual backend deployment identity, provider accounts/controlled send-and-receive destinations, HTTPS callback access, container runtime, Apple signing/TestFlight access for any needed release, and docs/site publication credentials. Locate configured resources without exposing secrets; ask for missing operational facts rather than inventing them.
