# Faxbot configuration and provider activation

Status: selected primary implementation design, independently challenged and clarified, under the approved whole-product refresh. The client contracts were inspected at dcd4c442. The bounded path repair is independently approved and image-verified at48c9ff96. This document records the selected design; it is not a claim that these changes are implemented.

## Outcome

An operator can edit, validate, apply, save, reload and restart configuration without losing credentials, partially changing the process, selecting a different provider by accident, or redirecting historical fax operations to another account. Settings, plugin configuration, diagnostics, dispatch and callback verification share the same validated source. Existing web, SDK, MCP and iOS request shapes remain supported.

## Selected storage and transaction design

Use immutable configuration revisions and immutable provider profiles in the existing application database. One singleton installation row identifies the active revision and any desired pending revision. Store secret-bearing revision/profile payloads encrypted with an installation key kept outside the database in a private persistent file or explicit deployment secret. This is a dedicated installation, not a new multi-tenant control plane.

The database is the activation authority. Files are bootstrap/import/export artifacts. Making both a JSON file and a database pointer authoritative would require a cross-store recovery protocol to decide which account accepted a job after a crash. Avoid that split.

PUT applies durably. The current UI already follows PUT with reload; keep both routes, and change UI wording to make durable Apply explicit. The existing Save endpoint verifies and atomically exports a recoverable configuration artifact. That file alone is not a full installation backup: restoring historical jobs and profile identities requires the database plus its original installation key. Do not introduce volatile live/saved heads that revert when one of several workers happens to restart.

A revision records normalized typed values, explicit directional-selection flags, complete plugin settings/enabled state, provider profile references, parent/revision identity, actor and timestamp. The active pointer changes in a short database transaction with compare-and-swap against the revision on which validation was based. Pending edits compare both the active generation and desired revision, so two editors cannot overwrite each other through an unchanged active pointer. Concurrent conflicting edits return a conflict instead of overwriting unseen changes. An unchanged patch is a no-op. No provider network call or file publication occurs while the configuration row is locked.

Credential payloads never appear in ordinary settings responses, audit records, exception messages or database diagnostics. Key creation is exclusive and private; startup must refuse to invent a replacement key when encrypted configuration already exists. Backups must include the installation key through a separate protected backup path. Encryption is operational containment for database copies, not a claim that a compromised application host cannot read credentials.

## Bootstrap and migration

Bootstrap inputs locate the database, data directory, installation encryption key and packaged/configured resource directories. They must be available before the canonical database can be read. Ordinary hot settings cannot change these underneath active requests.

On the first canonical configuration initialization only, import the complete validated environment plus the existing enabled persisted settings file, then reconcile an actually existing legacy plugin JSON document. A missing file's fabricated DEFAULT_CONFIG is not an operator selection and cannot create an import conflict. Persisted settings retain their current explicit precedence over environment when that feature is enabled. An existing malformed/unreadable enabled file fails startup; never silently substitute another provider's environment credentials. Migration must report contradictory legacy plugin/environment selection rather than guess which account was intended. The operator receives an explicit reconciliation path; existing files are retained.

After initialization, restart loads the canonical database revision. An explicit import operation previews and validates deployment environment or a saved file before creating a new revision; process environment changes do not silently overwrite managed settings. Document this migration in the generated operator docs and expose source/revision in the UI. Preserve the old settings routes as adapters, not alternate stores.

Changing DATABASE_URL is a datastore transfer, not live configuration. Preserve the deployment setting and provide the maintenance transfer procedure using verified backup/restore, installation identity and the original encryption key. The settings UI must describe this and cannot claim that a successful text edit moved jobs or users. Until the transfer workflow is implemented, reject a changed live database target explicitly rather than mutate os.environ or rebind requests to an empty store. The whole refresh release still requires the tested transfer/restore workflow.

## Value model and compatibility

A single immutable typed model owns environment aliases, defaults, secret metadata, validation and serialization. The detailed implementation sequence is `../plans/2026-10-02-faxbot-configuration-values.md`.

Retain GET /admin/settings's nested masked representation and PUT's flat patch. Omitted/null fields preserve values; explicit empty strings clear strings/secrets and reset directional overrides to inherited selection. Preserve false and zero where valid. Reject unknown inputs, invalid ranges, unknown providers and mask placeholders. The entire candidate is validated before mutation. GET /admin/config remains usable by the current login flow and reports actual active runtime selection.

Maintain existing _meta keys and add revision identity, desired revision, pending fields and apply state. The settings editor displays desired values with a prominent pending-restart explanation where relevant; diagnostics and dispatch use the active revision. No "applied" label for a staged candidate.

All enabled and inactive provider settings persist. Export cannot branch only on the legacy FAX_BACKEND. Values in files are literal data: no shell expansion, command execution or arbitrary environment injection. Display exports are redacted; privileged recovery exports contain the complete intended data and have private file permissions. Existing Save {} remains supported. Arbitrary host paths are not part of ordinary configuration authority; custom path/content requests are validated against the configured recovery target and authorized separately from routine settings editing.

## Activation and runtime ownership

Classify settings explicitly:

- Request/operation values can activate by changing the database revision. Each operation captures one immutable revision; it does not reread mutable globals halfway through submission, validation or document handling.
- Provider profiles are immutable and selected by revision. New operations use the new selected profile. Existing operations retain their original profile.
- Lifespan-owned resources, such as embedded MCP mounts/OAuth wiring, listener topology and audit sinks, require a coordinated restart unless the resource has a tested live replacement implementation.
- Bootstrap storage/database/key locations require their supported maintenance workflow.

If a patch changes any restart-bound field, stage the entire candidate; leave the active revision unchanged, including otherwise hot fields in the same patch. Subsequent edits operate on the desired candidate and revalidate it. Reverting all restart differences permits ordinary atomic activation. Reload refreshes the active/desired database view; it never silently promotes a pending candidate.

Promote a restart candidate only during a whole-installation stopped start. The supported deployment already shares local fax storage between API workers on one host. Use an installation lifecycle lock in that persistent directory: serving workers retain shared ownership, and candidate promotion requires exclusive ownership before resources initialize and the active pointer changes. Concurrent starters attempt exclusive startup ownership before taking shared serving ownership; they must not all take shared locks and then strand promotion by trying to upgrade. A rolling worker restart cannot obtain exclusive ownership and therefore cannot promote pending settings. It continues the active revision with pending state visible. The documented supervisor/container restart stops all workers before starting them. Do not claim cross-host coordination from a local lock.

Initialize the candidate's owned resources before marking it active/ready. On failure, retain the old active pointer and pending error state with sanitized diagnostics. Recheck candidate revision before commit; a concurrent change cannot be promoted accidentally. Acquire lifecycle ownership in each actual worker lifespan, never through an inherited pre-fork descriptor. Keep the immutable bootstrap lock path and lock ownership continuous while promoting/downgrading so no second candidate can start between exclusive and shared ownership. Release/downgrade exclusive lifecycle ownership only after the committed runtime is ready. A process that failed initialization does not serve requests. Restart-sensitive credentials/listeners cannot be promoted while doing so would orphan unresolved provider work; the profile/worker layer must either retain required resources or give an explicit actionable drain/reconciliation requirement.

## Provider and account identity

A provider name alone is not an account identity. A profile includes provider ID, a local immutable account identity, credential revision, endpoint/callback settings, relevant sender identity and manifest snapshot/digest. Store the manifest used by accepted work; later installation edits cannot rewrite its historical behavior. Never interpret changing an API key as proof that it belongs to the same remote account.

Credential edits create a new immutable credential profile. Keeping the same logical account across rotation requires a provider-supported account identity check or an explicit operator account association with clear evidence. Old profiles remain available while referenced by unresolved work. No API response exposes their raw credentials. Revoked remote credentials may require explicit operator reconciliation; they cannot authorize substitution of a different account.

Upload preparation captures one profile/traits frame before conversion. Acceptance rechecks that exact binding under the installation-row lock; if an incompatible selection changed, it either prepares again under the newly captured frame or returns a retryable conflict without accepting a job with the wrong document form. Accepted jobs bind to the active revision/profile in the same database transaction that makes them durable. Configuration activation and acceptance serialize through the installation row so a job sees one definite selection. All dispatch, status retrieval and callbacks use the stored profile. A small adapter cache keyed by immutable profile identity is safe; a global cached service with reload_settings is not.

Terminal historical jobs return their stored outcome without polling a newly configured account. Preexisting jobs have no provable account profile. Preserve their rows and documents; do not fabricate an identity during schema migration. Unresolved legacy jobs enter a visible reconciliation workflow in the durable-worker slice. Binding them requires evidence from their original provider/account and must never blindly resubmit an ambiguous fax.

Inbound events are verified and associated with a provider/account profile before durable acceptance. Rotating webhook credentials must keep the explicitly permitted prior verification generation for unresolved expected callbacks, with bounded account-scoped matching and deduplication. It must not try arbitrary current credentials and then declare an old event trusted. Provider-specific inbound behavior is completed with the inbound worker/adapters, not simulated in configuration code.

## Plugin and trait consistency

One resolved provider directory is used by install, traits, discovery, send preparation, dispatch and refresh. Registry metadata is not a provider. A missing/invalid registry or explicit unknown provider produces a clear configuration error rather than fallback to SIP. Validate trait types; the string "false" must not become true.

Plugin GET/PUT/list retain their existing shapes. Omitted/null enabled preserves its state; false disables new dispatch for the selected role without falling back to another provider or disabling the other hybrid direction; omitted/null settings preserves; an explicit empty settings object clears according to the plugin's validated schema; a nonempty settings object merges validated fields, preserving omitted secrets. Saving inactive settings does not activate the provider. Secret masks mean unchanged only through the documented UI adapter, never an actual stored credential. Installed-manifest credentials and settings feed the same immutable profile used by runtime execution. A successful file install is distinct from activation and from a successful real provider validation.

## Implementation order and completion evidence

1. Complete bounded resource-path repair and independent/image review.
2. Implement the typed values, safe literal persistence and settings projection foundation with failing regressions from the current export/load defects.
3. Add versioned configuration/profile tables, encryption-key lifecycle and first-import/reconciliation. Extend schema validation by revision; preserve frozen historical metadata and all prior upgrade proofs.
4. Implement canonical update/read/reload/export and restart staging/activation. Verify conflict handling and real multi-process lifecycle behavior on SQLite and PostgreSQL.
5. Bind accepted jobs/provider calls to immutable profiles. The durable outbound and inbound slices complete recovery and callback state transitions; configuration is not marked fully complete while those integration checks are missing.
6. Complete UI fields/status wording, retained configuration adapters, settings authorization through the RBAC policy, generated schema/docs and actual image restart/restore proof.

Required tests include complete synthetic hybrid export/restart, omitted/null/empty/false/zero semantics, invalid mixed patches leaving everything unchanged, concurrent updates, worker restart versus stopped-deployment promotion, failed resource initialization, lost DB commit acknowledgement, missing/wrong key refusal, provider/profile change between upload and acceptance, old-job lookup after rotation, disabled plugin behavior, and real source/container path identity. External account validation and real fax delivery remain separately recorded release gates.
