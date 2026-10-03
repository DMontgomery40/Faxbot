# Settings

Settings shows and changes the installation's saved configuration. Some changes take effect at once; others wait until Faxbot restarts. While a change is waiting, the screen says **Restart Faxbot to apply** followed by the number of pending changes, and the value shown can differ from the one Faxbot is using.

## Edit and apply

1. Click **Load Settings**.
2. Change only the fields you mean to update. Hidden secrets stay as they are unless you type a replacement or clear the field.
3. Click **Apply settings**. Only the fields you changed are sent.
4. Read the result. The message says whether the change is already in use or waits for a restart.
5. If it waits for a restart, stop every Faxbot API process and start the installation again, then click **Load Settings** to confirm the restart message is gone. Restarting one process while others keep running is not enough.

If someone else saved settings after you loaded them, Faxbot refuses your save and keeps your edits on the screen. Click **Load Settings**, review the current values, and apply again. If the console cannot confirm whether a save went through, load settings before trying again; the console never retries a save on its own.

The `.env` file and the legacy plugin JSON file are read once, when a new installation starts for the first time. After that, use this screen; later edits to those files are not imported. Container ports, mounts and telephone trunk settings stay in the deployment configuration.

## Provider directions and disabled sending

The default provider, the outbound override and the inbound override are separate choices. An empty override uses the default provider. Choosing an inbound provider does not turn receiving on; use its own switch. Providers installed from a manifest are configured in **Tools → Plugins**.

**Disable outbound fax sending** keeps accepting new faxes but holds them instead of sending. Turning sending back on never sends held faxes automatically, and pausing cannot recall a fax that is already being sent. Read [Test Mode](../setup/test-mode.md) before testing.

## Available controls

- Provider credentials and addresses, including Phaxio's separate callback token and outbound signature check.
- HTTPS enforcement, audit logging, and request and upload limits.
- Local or S3-compatible document storage, receiving, retention and download link settings.
- The MCP server built into the API, and its OAuth settings. Standalone MCP servers have their own launch settings.

Moving the database or changing installation paths is maintenance work that this screen does not do. The database address is shown hidden and cannot be changed here; keep the database, the installation key file and stored documents safe during maintenance.

## Export and recovery

**Export .env** returns a template of the saved settings with secrets hidden. It does not change anything on the server and is not a backup by itself. **Write recovery .env**, when turned on, writes a private recovery file on the server; it does not apply pending changes. A complete backup also needs the database, the installation key file and stored documents.

## API

- `GET /admin/settings`: saved values with secrets hidden, plus `_meta`, which says whether changes are waiting for a restart and lists them in `pending_fields`.
- `PUT /admin/settings`: the changed fields plus the `expected_revision_id` from the last read. The reply says whether anything changed and whether a restart is needed. Read the settings again for the new values.
- `POST /admin/settings/reload`: reads the saved settings again. It does not import the `.env` file or apply pending changes.
- `GET /admin/settings/export`: the `.env` template with secrets hidden.
- `POST /admin/settings/persist`: writes the recovery file.
- `POST /admin/settings/validate`: checks credentials you supply for the built-in providers that support it. It does not send a fax.

Reading settings and exporting the template need `settings:read`; saving needs `settings:write`. Provider fields need `providers:read` to see and `providers:write` to change, and some fields can only be changed by an Owner. Permission to save does not include permission to read. See [Access Control](../security/access-control.md).

See the [Setup Wizard](setup-wizard.md) for a guided edit and the [provider guides](../setup/index.md) for what each provider needs.
