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

## Hosted checkpoint and fresh documentation

Commit `f8afb9f8856b910301f0bc2f7d619703ce4ce6ba` was pushed. Docs Autopilot run `37086574927`, push MkDocs run `37086575149`, and PR MkDocs run `37086577766` succeeded. The push artifact was downloaded unchanged; its provenance names this exact commit, `source_tree_dirty=false`, and matching configuration/runtime source hashes. Primary Browser navigation from its generated overview to Configuration displayed that commit and the typed field table. This is a branch preview, not publication to main or faxbot.net.

CI run `37086577522` built the Admin UI successfully but the API suite reported 51 failed, 665 passed, 14 skipped, and 33 errors. Investigation is separating outdated test setup from product defects. Confirmed examples include mutation of the now-immutable settings facade, tests reusing canonical installation state while expecting environment changes to replace it, and document failure injection targeting the old ORM acceptance path. These failures are not waived. The immutable database and AMI test migrations retain their assertions; four database cases and five AMI lifecycle cases pass independently. Full CI remains a release gate.

## Send configuration and queue-only intent

Real GUI inspection found a hardcoded 10 MB limit after the active limit had been set to 13 MB, and normal-send language on a disabled-sending instance. The repaired Send screen refreshes the active configuration on entry, displays the actual limit, and identifies queue-only behavior. Primary Browser file-chooser checks selected 11 MB successfully, selected 14 MB and observed the 13 MB validation error, then clicked Queue with the oversized file and observed refusal. Selecting the small synthetic document and clicking Queue created `941bb5a59fb841639473f88ac92f1020`; Jobs and its detail dialog showed queued status, three pages, and the original filename.

Independent review caught a concurrent-mode-change risk before commit. The UI now sends an explicit `queue_only` multipart condition, enforced by the server before preparation. The primary retained a queue-only form while a second real browser changed the setting and a full fixture restart activated sending. Clicking the stale Queue button displayed the refusal; Jobs still contained exactly the same two jobs. No fax was dispatched. The second browser restored disabled sending and another restart activated revision `f393b49a-417b-4223-8da9-8a94b9229164`, generation 13. Both browsers verified the restored mode and 13 MB limit. The corrected bundle is `index-DQatsOd9.js`.

Future durable dispatch must also honor the immutable acceptance revision's `fax_disabled=true`: these queued test acceptances must never become normal deliveries merely because the installation later enables sending.


## Clean canonical-runtime CI and docs checkpoint

At `f50fb62d80ab870b58f3b9d37373cebb480a69d6`, hosted CI run `37088340367` passed **741 tests with 14 dialect skips**, plus the Admin UI build. The preceding failures were repaired by migrating legacy test setup to isolated canonical installations and immutable configuration frames. Document durability tests now inject faults at the actual acceptance transaction and reconcile durable rows/bindings independently; their failure assertions were retained. No failing check was waived.

Docs Autopilot `37088337673`, push MkDocs `37088337745`, and PR MkDocs `37088340651` all succeeded. The downloaded push artifact identifies the exact commit and `source_tree_dirty=false`. Primary Browser clicks from the generated overview to Typed configuration verified the matching source revision and field reference. This is a verified branch artifact; main and faxbot.net publication remain release work.

## Provider override and shell navigation acceptance

Primary Browser keyboard entry, Validate and Install created a synthetic HTTP manifest overriding the built-in `freeswitch` identity. With sending disabled, selecting that provider and queuing the original synthetic document produced job `e34b8fff199549a39e0f890d9c653814`. The Jobs screen displayed three pages and queued status. Supplemental read-only storage inspection confirmed no unnecessary TIFF and an immutable accepted revision/profile binding. This verifies document acceptance, not external delivery.

The same interaction exposed duplicate provider cards and inherited native-provider descriptions; those findings are tracked as GUI014 for correction and replay. A separate App-only correction, `09f57f84dfcf25cc6914959b55cbdb594c509139`, passed real browser checks for feature changes: Plugins appeared/disappeared on entering Tools without reload, Scripts selection survived its appearance, and disabling a selected Plugins tab returned to Terminal. Inbox and queue-only Send remained truthful. The synthetic installation was restored to enabled plugins, Phaxio, disabled sending and a 13 MB upload limit.


## Plugin editor and inventory replay

Plugin UI adapter `ac28eb50` carries the loaded desired revision and explicit role for edits/selections, preserves untouched fields and explicit clears, and displays desired/active/pending state. Independent source review caught an outbound signature field mislabeled inbound; the corrected label was rebuilt and checked in the real GUI. Two actual browser editors proved stale list/dialog rejection, retained drafts, explicit reload recovery, immediate nonsecret save/clear, and a desired-only save while restart was pending. The fixture was restored to active=desired `02d62b20-b58d-4e09-a861-ee406a123b82`, generation29, with sending disabled and upload13.

Primary IAB replay after restart confirmed one synthetic manifest override card with no native description/link and intact Local/S3 storage cards. Validate and Install refused a keyboard-entered manifest named local; bulk import refused s3 with zero imported and the reserved-storage reason. The catalog correction also restores the entire native definition when HTTP plugins are disabled; 232 focused internal catalog/activation checks passed on SQLite/PostgreSQL after reproducing eleven failures.

This replay found two further UI/server gaps, tracked as GUI015–017: a terminal stuck at Connecting, and manifest errors hiding useful details. The control labeled Dry-run Send also called the real provider operation and bypassed disabled sending. The server now refuses that operation when sending is disabled and checks the administrative execution gate before opening a terminal. Primary clicked the still-loaded old send control with a harmless synthetic.invalid recipe and observed409 refusal. Corrected terminal/error/test-send presentation is undergoing a separate bounded UI repair and replay. No external fax was transmitted.

## Execution controls and fresh pushed documentation

At `f69557857fe5913b4171620e7c7b587c885c9347`, hosted CI `37090220668` passed **755 tests with 14 dialect skips**, plus the Admin UI build. Docs Autopilot `37090218512`, push MkDocs `37090218684` and PR MkDocs `37090220730` succeeded. The downloaded push artifact has matching source SHA, `source_tree_dirty=false`, and the reviewed main.py hash. Primary real Browser navigation Home → Reference → Generated source visibly confirmed that exact revision.

UI correction `56732060` passed its build and real-browser replay: Terminal displays disabled and Reload availability settles to the same state without opening a shell; manifest Validate/Install display the reserved-storage explanation; bulk invalid imports show a red zero-imported/two-failed result; real test sending is explicitly named and disabled under active disabled-sending configuration, while render-only validation remains usable. Review caught and corrected display changes reconnecting terminal sessions; theme/responsive settings now update the existing display independently. The final bundle is `index-8wmWAEQc.js`. This evidence covers disabled/error flows, not a verified enabled terminal or real transmission. The untracked manifest transmission path must still be integrated with the durable delivery module before release.
