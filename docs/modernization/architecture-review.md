# Faxbot architecture review — current state

Updated October 3, 2026, at the user's handoff request. Source checkpoint: `a14a23e1857a52337bb2c7353db6845c014272eb`, branch `feat/faxbot-refresh`, draft [PR32](https://github.com/DMontgomery40/Faxbot/pull/32). The modernization is **unfinished**. Goal mode is paused and the subagents are interrupted. This is a factual architecture/status review, not an implementation sequence.

## Product requirements

The target remains a finished self-hosted/on-premises Faxbot for the October 7, 2026, 10:00 AM Mountain meeting. Complete RBAC, retained clients, real fax delivery and transferability remain part of the objective. The user permits UI improvements; the UI is not frozen.

**Latest user instruction: stop putting developer, log and chat-style text into the product UI, and remove the clutter already introduced.** Full revision UUIDs, generation counters, repeated implementation explanations and stacked caveats were exposed throughout Settings, Setup, Plugins, MCP, Logs and other screens. Their presence in previous GUI acceptance does not mean the user accepted that design. Cleanup has not been performed during handoff.

Faxbot.net is marketing/demo/documentation, not the runtime API. The iOS companion is in TestFlight and connects to the operator's instance over WireGuard, Tailscale or Cloudflare Tunnel. DNS migration was not performed. No production deployment, main merge or real external fax completion has been established.

## Change size and source identity

- Base main: `7e0c15fa960d24b59c7581eeb1163b3d93439e32`.
- Current checkpoint: 144 commits, 295 changed files, 45,696 insertions and 6,437 deletions relative to that base.
- Implementation worktree: `/Users/davidmontgomery/.codex/worktrees/faxbot-refresh/Faxbot`.
- Original checkout: `/Users/davidmontgomery/Faxbot`; it is not the implementation worktree.
- Branch and origin match the checkpoint. PR32 is open and draft.
- This review refresh is an uncommitted documentation-only update after that checkpoint.

## Current architecture and integration boundaries

| Area | Implemented in the branch | Remaining boundary or missing proof |
| --- | --- | --- |
| Document handling | Real PDF/TIFF conversion, input validation, safe artifact handling and uncertain-commit preservation | Browser download/save/render completion is unresolved (GUI008); full inbound acquisition/retry path remains incomplete |
| Runtime and schema | Reconciled API/Python MCP dependencies; owned startup/shutdown; additive versioned SQLite/PostgreSQL upgrades and historical preservation checks | Older image proofs do not verify the current complete release; final installation/upgrade/backup/restore evidence absent |
| Configuration | Validated immutable DB revisions; encrypted installation records; active/desired state; restart staging; CAS edits; captured provider profiles | Validation/probes, provider install/import and recovery publication still have legacy integration gaps |
| Outbound delivery | Persisted jobs/attempts, worker leases, conservative ambiguous-outcome handling, captured callbacks and original-account polling; held faxes do not auto-send | Controlled real fax delivery/receipt and complete native/provider operation remain unproved |
| Access policy | Current principal/resource policy, credentials, sessions, admission, typed versioned identity mutations, common resource visibility | Named management HTTP/UI, mailbox routing, legacy key integration and complete route/client coverage are unfinished |
| HTTP integration | Auth routes; current-authority outbound operations; permission-scoped settings/provider reads and writes; safe registry projection | Many privileged/inbound/tunnel/terminal/MCP surfaces retain unfinished conversion; no completed registered-route coverage checker |
| Console | Settings/Setup/Plugins/MCP/Logs integrate canonical configuration and distinguish saved writes from later read failure | Legacy API-key localStorage shell; incomplete capability navigation and management views; user-rejected technical clutter |
| Retained clients | Existing iOS/SDK/MCP/desktop sources inventoried; some Python MCP runtime work | Current iOS/TestFlight, SDK, desktop, pairing and distinct-client identity acceptance absent |
| Documentation | Source-derived API/reference generation and preview provenance; main publication workflow repaired | Fresh branch preview is not public main publication; maintained prose is not automatically fixed by a green proposal run |

Configuration and accepted delivery now have distinct ownership. A settings edit creates a validated revision; accepted faxes keep their captured provider/account state. Human authorization is checked against current identity and policy. That architecture is implemented in significant parts of the runtime, but the still-legacy management and privileged paths prevent a complete RBAC claim.

The clearest current integration defect is `/admin/api-keys`: it still creates legacy key rows without the new principal/binding state, so those newly issued credentials fail modern authentication. Internal typed mutations exist but are not exposed through a complete management workflow. Free-text `owner` is not an identity or resource grant.

## Verification status

User-facing acceptance is required to use actual Browser/Computer input. Internal tests, hosted HTTP regression tests, source review, GUI observations, real provider delivery and deployment are separate evidence categories.

| Source | API CI | UI/docs |
| --- | --- | --- |
| `05f0ed40` | 34 failed; 2,906 passed; 24 skipped | Historical checkpoint |
| `d2c2212f` | 35 failed; 2,990 passed; 24 skipped | UI and docs passed |
| `66333d7a` | **8 failed; 3,089 passed; 24 skipped** | UI and docs passed |
| `a14a23e1` | In progress at the review's status check, [run 37126724299](https://github.com/DMontgomery40/Faxbot/actions/runs/37126724299) | Hosted UI and fresh Mike/Autopilot runs passed; local exact archive UI build passed |

At663, four failures were HTTPS/origin fixture drift and four were real legacy-key/current-identity gaps. The a14 change repairs only the four hybrid fixture transports; the latest API run had not finished when checked. An earlier green run is not current-head evidence.

Actual latest GUI observations: retained provider draft after a lost response, disabled repeat Save and explicit reload; restored original callback setting; truthful already-enabled audit state; corrected MCP labels; safe Swagger empty patch and sanitized422 response. The ordinary provider setting was restored at generation 58. No fax was sent during these checks.

The narrow Logs test still found clipped action switches and an empty displayed Custom selection (GUI064/065). Assigned fixes were interrupted before any tracked source edit. The final synthetic server is still a labelled fault-overlay launch with its fault controls cleared. It has not been restored to the ordinary final launch.

## Documentation and local preview

Fresh generated documentation for the committed checkpoint is available at [a14a23e1 review preview](http://127.0.0.1:8858/review/a14a23e1/). The watcher verified source provenance, artifact hashes and generated schema/control-label corrections. Primary fresh-site Browser replay was interrupted. The older 663 guide was the visible tab when handoff began.

The synthetic app at `http://127.0.0.1:8877/admin/ui/` runs exact a14 source, but its configured docs link still points at `/review/66333d7a/`. The public latest site remains separate and has not published this branch. The website repository is referenced by historical workflows as `DMontgomery40/faxbot.net`.

## Execution failures and accumulated rework

The previous agent allowed the change to expand across 144 commits while the product remained incomplete. Multiple small commit/docs/review/replay cycles consumed substantial usage. Configuration UI repairs required further repairs; API schemas and maintained docs repeatedly contradicted actual behavior; strict transport integration was followed by several waves of incomplete fixture migration. Security/transaction review caught substantive defects before their bounded slices were approved. PDF download attempts repeated an unresolved native Save handoff. The latest responsive test initially captured a normal viewport as narrow before the mismatch was detected and corrected.

The user reports40% of their $500/month plan usage consumed in 12 hours and stopped the agent. The goal counter reported 29,555,116 tokens at pause; that is not an independently reconciled billing figure. The detailed handoff records known mistakes, evidence limitations, exact runtime state and unfinished scope. It does not prescribe the successor's implementation order.

## Unfinished product scope

Complete management HTTP/UI and cookie console authentication; inbound mailbox/resource integration; all privileged-route enforcement; terminal/tunnel/pairing and MCP client identity; retained iOS/desktop/SDK behavior; real provider/native fax delivery; current dependency/release/container and backup/restore proof; public docs publication; and removal of the developer/log/chat UI pollution remain unfinished. No completion percentage is established.

## Local handoff and evidence

Detailed takeover prompt: `/Users/davidmontgomery/.codex/worktrees/faxbot-refresh/Faxbot/.superpowers/sdd/2026-10-03-faxbot-handoff/FAXBOT-HANDOFF-2026-10-03.md`.

Complete chronological commit history and diffstat are beside it. The ignored `.superpowers/sdd/` evidence directory is local-only and is not included in a clone of the branch. The canonical UI scratchpad is `.superpowers/sdd/2026-10-02-faxbot-configuration/gui-bug-scratchpad.md`. Some historical headings are stale; the handoff supplies the latest interrupted observations.

The original October 2 main assessment is retained in Git history and in the local handoff archive as `initial-assessment-before-handoff.html`. Its 27-test baseline, absent Ghostscript, proposed architecture and unresolved deployment assumptions are historical, not current facts. The approved specifications remain requirements records; they are not proof of completed integration.
