# Canonical configuration runtime checkpoint

This checkpoint integrates the reviewed immutable configuration modules into the actual API. It is not the full Faxbot release acceptance record.

The API worker now owns its installation lock, encryption key/store, resource preparation and coordinated pending promotion. Each request captures one immutable active revision. Settings and plugin adapters apply complete candidates durably, preserve omitted/empty/false/zero semantics, report desired versus active identity and reject stale editor revisions. Reload reads canonical state. Exports cover inactive and hybrid provider values; recovery writes are confined to the configured private artifact. Database transfer remains a maintenance operation.

First import reads ordinary enabled persisted values with their existing precedence. Bootstrap DATABASE_URL and FAX_DATA_DIR must already identify the same locations as a legacy file; an explicit mismatch fails with reconciliation guidance before importing. After initialization, changed or malformed ordinary environment/import artifacts cannot overwrite the database revision. This is a deliberate bootstrap boundary, not an automatic datastore transfer.

Accepted outbound jobs and immutable profile bindings commit together. Preparation uses captured traits; configuration changes invalidate stale preparation. Dispatch constructs adapters from the accepted profile, with no unknown-provider fallback to SIP. Uncertain commit acknowledgements retain documents and identify the job for reconciliation. Terminal refresh uses stored results; unresolved legacy work cannot silently poll a replacement account. Durable queue recovery and complete outbound/inbound callback binding remain required release work.

Embedded MCP factories receive explicit startup OAuth/topology settings and sample canonical API credentials per tool invocation. Audit sinks close on shutdown/failure. Repeated cancellation joins startup ownership work before releasing locks. Restart refuses to orphan unresolved work that depends on the old AMI connection. Storage clients retain their captured bucket, prefix and encryption settings.

## Browser acceptance

User-facing checks used actual Browser clicks and keystrokes on the isolated self-hosted fixture at localhost:8877, with fax sending disabled and no real provider credentials. The primary used Codex In-app Browser; the Sol UI agent used its independent Brave tab. No user-facing endpoint request was used as a substitute for GUI acceptance.

- Applied upload size 12, then refreshed and reloaded the browser; the value persisted. Global rate limit zero and disabled inbound remained unchanged.
- Set and explicitly cleared a synthetic callback URL; the separate public API URL remained unchanged. Set and cleared a directional override to inheritance.
- A blank numeric draft was rejected visibly without changing the revision.
- Changed an inert MCP SSE path while both transports stayed disabled. The editor displayed desired `e8aa6fe0-633d-4454-a97e-1eb7c9f62b71`, active `a5f89134-350a-437b-a712-0fdd9bf5446a`, generation 7, and pending restart; it did not label desired controls as active.
- Stopped the synthetic worker and started the installation again. Clicking Load Settings showed the same desired revision now active, generation 8, with upload size 12 retained.
- The primary applied upload size 13 through real keystrokes and Apply, producing revision `3baa55a9-8687-4624-9019-8a40edffb978`, generation 9. The second tab, still holding generation 7, tried upload 14 and received the visible stale-editor refusal. Load Settings restored 13; no overwrite occurred.
- Export and Write recovery .env showed the redacted display artifact and private recovery success without promoting pending settings. The database URL stayed opaque and read-only. Browser warning/error logs were empty.

The exact UI commit is `760a8320`; the built and rendered bundle is `index-BTMxKzsb.js`. Detailed DOM captures, screenshots, revisions and defect/replay records are retained in the local configuration scratchpad and `canonical-settings-ui-report.md`.

## Internal checks and independent review

The combined configuration/profile/store/lifecycle/storage/MCP run passed 123 tests, including SQLite and PostgreSQL store/activation coverage. These are internal regression checks, not GUI or external-delivery proof. Independent review reproduced and verified corrections for audit handler ownership, frozen per-route rate limits, repeated startup cancellation, disabled-fax AMI drain, custom AMI profile traits, post-commit response reads and raw refresh error disclosure. The pure settings projection and immutable provider/MCP factories were reviewed independently.

The branch is still a draft. Whole-product completion additionally requires the remaining durable worker and callback work, RBAC, all retained clients and tunnel/iOS workflows, transfer/restore, fresh full release checks, and controlled real fax delivery. No deployment, merge, provider account validation, external delivery or healthcare compliance conclusion is claimed by this checkpoint.
