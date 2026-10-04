# Saving settings

Each settings page (In use, a provider's page, Email delivery, Security, Storage & retention and the others) shows and changes the installation's saved configuration. Some changes take effect at once; others wait until Faxbot restarts. While a change is waiting, the screen says **Restart Faxbot to apply** followed by the number of pending changes, and the value shown can differ from the one Faxbot is using.

## Edit and apply

1. Open the page. **Reload** reads the saved values again.
2. Change only the fields you mean to update. Hidden secrets stay as they are unless you type a replacement or clear the field.
3. Click **Apply settings**. Only the fields you changed are sent.
4. Read the result. The message says whether the change is already in use or waits for a restart.
5. If it waits for a restart, select **Restart now** in the restart message. Faxbot stops, Docker Compose starts it again, and the screen loads the settings again and says "Faxbot restarted and is using the saved settings." The button appears when your role may restart Faxbot (`host:restart`) and the installation allows it (**Allow restarting Faxbot from here** under System → Diagnostics); otherwise the message says to run `docker compose restart api` on the server. Where several API processes run, stop every one of them and start the installation again; restarting one process while others keep running is not enough.

If someone else saved settings after you loaded them, Faxbot refuses your save and keeps your edits on the screen. Select **Reload**, review the current values, and apply again. If the console cannot confirm whether a save went through, load settings before trying again; the console never retries a save on its own.

The `.env` file and the legacy plugin JSON file are read once, when a new installation starts for the first time. After that, use this screen; later edits to those files are not imported, except credentials. Container ports, mounts and telephone trunk settings stay in the deployment configuration.

## Credentials from .env

Provider keys, passwords and other secrets set in `.env` are read every time Faxbot starts, and the value there is the one in use. Such a field shows **Set in .env** and cannot be edited or revealed here: change it in `.env`, then run `docker compose up -d`, because a plain `docker compose restart` keeps the container's old environment and never reads the new value. A changed value is saved as a new settings version by "environment", and the security audit names the setting, never its value. Faxes accepted earlier keep the account they were accepted with. If you remove the variable, the stored value stays and becomes editable on this screen again. `API_KEY` (the installation key) and `DATABASE_URL` keep their own rules: the installation key is read only at the first start, and moving the database needs the maintenance transfer.

Carrier names are accepted too: `HUMBLEFAX_API_ACCESS_KEY` and `HUMBLEFAX_API_SECRET_KEY` for the HumbleFax keys, and `TELNYX_SIP_PASSWORD` or `TELNYX_PASS` for the SIP trunk password.

## Provider directions and disabled sending

**Providers → In use** shows what sends and what receives, by name (the trunk by its carrier, such as **Telnyx**). Providers are chosen in the [Setup wizard](setup-wizard.md) (**Add or change a provider**). **Receiving is on** turns receiving on or off with the receiving provider chosen there; it cannot be turned on with a provider that only sends. Providers installed from a manifest are configured under **System → Developer → Provider plugins**.

Turning **Sending is on** off (it asks first) keeps accepting new faxes but holds them instead of sending. Turning sending back on never sends held faxes automatically, and pausing cannot recall a fax that is already being sent. Read [Test Mode](../setup/test-mode.md) before testing.

## Available controls

- Provider credentials and addresses, including Phaxio's separate callback token and outbound signature check.
- HTTPS for document links, event recording (on the Audit log page), and request and upload limits.
- Local or S3-compatible document storage, receiving, retention and download link settings.
- The MCP server built into the API, and its OAuth settings. Standalone MCP servers have their own launch settings.

Moving the database or changing installation paths is maintenance work that this screen does not do. The database address is shown hidden and cannot be changed here; keep the database, the installation key file and stored documents safe during maintenance.

## Export and recovery

**Export settings** (Storage & retention) returns a `.env` template of the saved settings with secrets hidden. It does not change anything on the server and is not a backup by itself. **Save a recovery copy** writes a private recovery file on the server; it does not apply pending changes. A complete backup also needs the database, the installation key file and stored documents.

## API

- `GET /admin/settings`: saved values with secrets hidden, plus `_meta`, which says whether changes are waiting for a restart and lists them in `pending_fields`.
- `PUT /admin/settings`: the changed fields plus the `expected_revision_id` from the last read. The reply says whether anything changed and whether a restart is needed. Read the settings again for the new values.
- `POST /admin/settings/reload`: reads the saved settings again. It does not import the `.env` file or apply pending changes.
- `GET /admin/settings/export`: the `.env` template with secrets hidden.
- `POST /admin/settings/persist`: writes the recovery file.
- `POST /admin/settings/validate`: checks credentials you supply for the built-in providers that support it. It does not send a fax.

Reading settings and exporting the template need `settings:read`; saving needs `settings:write`. Provider fields need `providers:read` to see and `providers:write` to change, and some fields can only be changed by the owner: the console shows those disabled, with one sentence, to everyone else (`owner_only` in `GET /admin/settings`). Settings set when Faxbot was installed are shown read-only (`deployment`), never with a secret's value. Permission to save does not include permission to read. See [Access Control](../security/access-control.md).

See the [Setup Wizard](setup-wizard.md) for a guided edit and the [provider guides](../setup/index.md) for what each provider needs.
