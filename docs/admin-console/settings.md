# Settings

Settings edits the installation's canonical **desired** configuration. The page shows the loaded desired revision, active revision, generation and pending fields. A displayed desired value can differ from the value currently used by requests.

## Edit and apply

1. Click **Load Settings** and review the loaded revision.
2. Change only the fields you intend to update. Leave stored secret masks unchanged to preserve them; enter a replacement or clear the field explicitly to change a secret.
3. Click **Apply settings (save durably)**. The editor sends changed fields with its loaded desired revision. A conflict retains your draft; explicitly load settings again and review before retrying.
4. Read the result. Applied changes are active and saved in the database. Pending changes are saved but do not take effect until every API worker stops and the installation restarts successfully.
5. After a coordinated restart, load settings again and confirm the desired revision is active. Restarting one worker while others remain running does not promote pending settings; the readonly Reload endpoint does not promote them either.

A confirmed save and the following editor reload are separate operations. If the
reload fails, the save remains confirmed; reload or sign in again before editing
further. If the save itself cannot be confirmed, inspect the current revision
before resubmitting. The console does not automatically retry configuration writes.

The process/container `.env` and legacy plugin JSON are bootstrap inputs for an installation without canonical state. Editing them and restarting does not override an existing canonical revision. Use the Settings editor for existing installations; keep deployment-only container/trunk settings in the deployment configuration.

## Provider directions and disabled sending

Default provider, outbound override and inbound override are independent. An empty direction override inherits the default provider. Choosing an inbound provider does not turn inbound handling on; use its separate enable control. Installed manifest providers can be configured in **Tools → Plugins** without resetting the default provider.

**Disable outbound fax sending** accepts new uploads as held jobs. Re-enabling sending never automatically transmits those held jobs. Pausing ready work cannot recall an attempt already issued. Review [Fax Disabled](../setup/test-mode.md) before testing.

## Available controls

- Provider credentials and URLs, including Phaxio's separate Callback Token and outbound signature flag.
- REST authentication, HTTPS enforcement, audit settings and request/upload limits.
- Installation-local or S3-compatible artifact storage, inbound enablement, retention and token settings.
- Embedded Python MCP HTTP/SSE and OAuth settings. Standalone Node/Python MCP processes have their own launch configuration.

A database target or installation path change is maintenance work, not a live datastore move. The database URL is displayed opaquely and is read-only; preserve the database, installation key and artifacts during maintenance.

## Export and recovery

**Export .env** returns a redacted template of desired settings. It masks secrets and does not activate configuration or restore the installation by itself. **Write recovery .env**, when enabled, writes the private desired recovery file on the server; it does not promote pending settings. A complete recovery backup also requires the database, original installation encryption key and artifacts.

## API contract

- `GET /admin/settings`: sanitized desired values plus `_meta` active/desired identity and apply state.
- `PUT /admin/settings`: changed canonical fields plus the loaded `expected_revision_id`; returns a confirmed receipt containing `ok`, `changed`, and `_meta` active/desired revision IDs, generation, apply state and restart requirement. Read the settings endpoint separately for values and pending fields.
- `POST /admin/settings/reload`: reads durable state; it does not import environment or activate pending changes.
- `GET /admin/settings/export`: redacted desired template.
- `POST /admin/settings/persist`: writes a recovery environment file; it does not become the authoritative runtime store.
- `POST /admin/settings/validate`: checks explicitly supplied credentials for supported builtins. Stored masks and a green presence check are not delivery proof.

Settings reads and redacted export require `settings:read`. A settings write
requires `settings:write`, with additional provider or complete-Owner authority
determined from the normalized changes. Provider configuration reads require
`providers:read`; writes require `providers:write` and the same change-sensitive
checks. Provider writes return the same bounded receipt. Write permissions alone
do not grant access to settings values, provider configuration or server paths.

See the [Setup Wizard](setup-wizard.md) for a guided edit and [provider guides](../setup/index.md) for deployment prerequisites.
