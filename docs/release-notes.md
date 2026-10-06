# Release notes

## October 2026: the Faxbot refresh

This release rebuilds Faxbot's backend, delivery and self-hosted administration. Existing installations upgrade in place: read [Upgrade an installation](deployment.md#upgrade-an-installation) before you start.

### For operators

- **People sign in as themselves.** Users have their own username and password; apps, scanners and phones are integrations with their own API keys. Roles, groups and mailbox access decide what each may do, and the server checks every request. The installation key (`API_KEY`) creates the [first owner](security/access-control.md#create-the-first-owner) and recovers access; it is not for daily work. See [Access control](security/access-control.md).
- **Console sessions need HTTPS.** Browser sign-in works over HTTPS or on the same computer. Plain HTTP works only when you set `FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS=true` on a private network you control. API keys work over any connection. See [HTTPS and plain HTTP](security/authentication.md#https-and-plain-http).
- **Settings live in the installation.** `.env` is read only when a new installation starts for the first time, except credentials. After that, change settings on the console's [Settings](admin-console/settings.md) screen or with `faxbot system settings`. When a change waits for a restart, stop every API process, start again, and check that nothing is waiting.
- **Credentials from .env.** Provider keys, passwords and secrets in `.env` are read at every start and are the values in force; the console shows them as **Set in .env**. Carrier names such as `HUMBLEFAX_API_ACCESS_KEY` and `TELNYX_PASS` are accepted. See [Credentials from .env](admin-console/settings.md#credentials-from-env).
- **Delivery routes and costs.** Add outbound routes, rate cards and destination preferences. Faxbot ranks routes by estimated price and delivery history, records each attempt, and keeps unknown charges unknown. See [Delivery routes](operations/delivery-routes.md).
- **Intake and the Inbox.** Every received document goes into one intake queue, and email delivery sends the PDF to the inboxes staff already use. The console's **Inbox** shows each fax with its email delivery and a retry action. An uncertain email outcome waits for a person. See [Intake](operations/intake.md).
- **Direct delivery.** Verified partner installations exchange encrypted original PDFs and signed receipts, with ordinary fax as the fallback. See [Direct delivery](operations/direct-delivery.md).
- **Your own SIP trunk.** Connect a carrier SIP trunk (Telnyx first) to the built-in Asterisk engine and fax over T.38, with a record of every call. A live carrier call has not been tested yet. See [SIP trunk](setup/sip-trunk.md).
- **No provider until you choose one.** An upgraded installation that never set `FAX_BACKEND` keeps using Phaxio; a new installation starts with no provider until you choose one. Sending is refused with "No fax provider set up yet." until then.
- **One destination per fax, in E.164.** Faxbot reads national numbers for the installation country (`FAX_DEFAULT_COUNTRY`, UK and US) and stores the full international number on the job.
- **A command line for everything.** `faxbot` covers the console's tasks. With Faxbot stopped, `faxbot system status`, `migrate`, `recover-owner`, `backup` and `restore` show the installation's state, upgrades its database, recovers owner access, and makes and restores backups. See [Command line](operations/cli.md).
- **Docker Compose.** `.env` is optional, so a new installation starts with defaults. The MCP containers no longer receive the installation key.
- **The Terminal is for owners.** Only owners can use the console Terminal by default. Host Operators no longer can, unless an owner gives them a role that includes it (**Access → Roles**). The **Audit log** records every terminal session.
- **Three permissions that did nothing are gone.** "Run server actions" (`host:actions`) and the two remote-access permissions (`tunnels:read`, `tunnels:manage`) no longer appear under **Access → Roles**. Upgrading removes them from every role and key, and the **Audit log** records what was removed. A key whose only limit was one of them can no longer do anything; give it the permissions it needs, or replace it.
- **Removed features.** The Remote access page and `faxbot system tunnel` never set up a tunnel, so they are gone. To reach Faxbot from outside your network, use your own domain or VPN. Phone pairing stays under **Access → Keys & phones**. The **Server checks** on Scripts & checks and `faxbot system actions` are gone; use the Terminal or Diagnostics instead. The provider list (`/plugin-registry` and `PLUGIN_REGISTRY_PATH`) is gone. If your settings still name `PLUGIN_REGISTRY_PATH`, Faxbot ignores it for one release. Setup and the console no longer offer FreeSWITCH, which could not run in the shipped Docker image. The Sinch setting **Check that received faxes come from Sinch** (`SINCH_INBOUND_VERIFY_SIGNATURE`) is gone, because Sinch's Fax API does not sign the faxes it sends, so the setting checked nothing. To have Faxbot check each received fax, set a user name and password for the Sinch webhook; without them, Faxbot confirms each fax with Sinch before it keeps it. If your settings still name `SINCH_INBOUND_VERIFY_SIGNATURE`, Faxbot ignores it for one release. The Sinch **Signing secret** (`SINCH_INBOUND_HMAC_SECRET`) is gone for the same reason. Faxbot looked for a signature that Sinch never sends, so with a secret set, Faxbot refused every fax from Sinch. If your settings still name `SINCH_INBOUND_HMAC_SECRET`, Faxbot ignores it for one release. Each fax is now checked with the webhook user name and password, or confirmed with Sinch. Source: [Sinch's guide to moving from Phaxio v2.1 to the Sinch Fax API](https://developers.sinch.com/docs/fax/v2-v3migration), read 5 October 2026.

### Renamed commands

Every `faxbot` command now sits under one of the console's eight areas, and the older names no longer work. Scripts that use an older name need the new one. The [command reference](reference/cli.md) lists every command.

| Older command | Now |
| --- | --- |
| `faxbot jobs …` | `faxbot sent …`; `jobs get` is `sent show`, `jobs history` is `sent evidence`, `jobs reconcile` is `sent confirm-receipt` |
| `faxbot inbound …` | `faxbot received …`; `inbound get` is `received show`, `inbound simulate` is `system diagnostics test-fax` |
| `faxbot work …` | `faxbot received …`; `work list` is `received owners`, `work show` is `received history`, `work settings` is `numbers mailboxes target` |
| `faxbot import` | `faxbot received import` |
| `faxbot routing …` | `faxbot recipients list`, `show` and `set` for destinations, `recipients together` for sending together, and `faxbot costs spending`, `reconcile`, `fax`, `rate-cards` and `plans` |
| `faxbot intake …` | `faxbot received deliveries list` and `retry`, and `faxbot numbers email connectors …` |
| `faxbot direct …` | `faxbot recipients partners …` |
| `faxbot cases …` | `faxbot recipients cases …` |
| `faxbot trunk …` | `faxbot providers trunk …` |
| `faxbot settings …`, `diagnostics …`, `logs …`, `health`, `restart` | `faxbot system settings …`, `system diagnostics …`, `system logs …`, `system health`, `system restart` |
| `faxbot admin …` | `faxbot system status`, `migrate`, `recover-owner`, `backup` and `restore` |
| `faxbot config …` | `faxbot system profiles …`; `config set-profile` is `system profiles save`, `config show` is `system profiles list` |
| `faxbot me`, `users …`, `integrations …`, `groups …`, `roles …`, `keys …`, `sessions …`, `owner …`, `resources …`, `pair …` | `faxbot access …`; each `get` is `show` |
| `faxbot access grant`, `access list`, `access revoke` | `faxbot access grants add`, `grants list`, `grants remove` |
| `faxbot mailboxes …` | `faxbot numbers mailboxes …` |
| `faxbot audit list` | `faxbot system audit` |
| `faxbot providers config` | `faxbot providers show` |
| `faxbot providers registry import` | `faxbot providers import` |

### Deprecated, removed in the next release

Each of these still works in this release and shows a notice where you use it. Stop using them before your next upgrade.

| Feature | Use instead |
| --- | --- |
| MCP over SSE: the Python SSE server, its container (`faxbot-mcp-py-sse`), `ENABLE_MCP_SSE` and the SSE switch under **AI assistants** | Streamable HTTP (`ENABLE_MCP_HTTP`, or `faxbot-mcp-py-http`) |
| FreeSWITCH as a fax provider | Another provider, chosen in the Setup wizard |
| Provider plugins: `FEATURE_V3_PLUGINS`, `GET /plugins`, `GET` and `PUT /plugins/{id}/config`, and the **Provider plugins** page | Built-in providers in Setup, and `GET` and `PUT /admin/settings` |
| The settings recovery copy: **Save a recovery copy**, `faxbot system settings persist`, `POST /admin/settings/persist` and `ENABLE_PERSISTED_SETTINGS`. New installations no longer turn it on; an installation that already has it on keeps it. | `faxbot system backup` and `faxbot system restore` |
| `client.plugins` in the Python and Node SDKs (it now warns once when used; the SDKs drop it in their next major version) | The console, or `faxbot providers` |


- **Existing fax routes keep their contract.** `POST /fax`, `GET /fax/{id}`, `GET /inbound`, `GET /inbound/{id}`, `GET /inbound/{id}/pdf`, `GET /health` and `POST /mobile/pair` keep their fields. Jobs gain `delivery_state`, `dispatch_mode`, `delivery_version` and `reconciliation_reason`.
- **New routes.** Sign-in (`/auth`), access management (`/access`), routes and costs (`/routing`), intake (`/intake`), direct delivery (`/direct`) and case packets (`/cases`). See the [API reference](api.md) and [Access and sign-in API](reference/access-api.md).
- **Safe retries.** Send an `Idempotency-Key` header with `POST /fax`. The same key with the same number, document and queue setting returns the original fax instead of sending another; the same key with a different request is refused with 409.
- **No automatic resend.** When Faxbot cannot tell whether a provider accepted a fax, the job waits for a person to check it (`faxbot sent confirm-receipt`). Faxbot never sends it again on its own.
- **Received documents need `inbound:document`.** Keys from earlier releases with `inbound:read` still list and read received faxes, but they no longer open the PDF. Create a new key for apps that need documents.
- **Unrestricted keys need review.** A key from an earlier release with every permission (`*`) does not work until an administrator approves it with an owner and a permission list (`faxbot access keys approve`).

### For SDK users

- **Stable send operations.** Python `send_fax(to, path, operation_id=...)` and Node `sendFax(to, path, { operationId })` send each fax with an operation id as its `Idempotency-Key`. Save the id before sending. When the result is uncertain, finish the same fax with `resume_fax` / `resumeFax`; the SDKs never resend on their own.
- **Provider plugins.** `install_plugin(manifest)` / `installPlugin(manifest)` take an HTTP provider manifest, not a plugin id. `update_plugin_config(plugin_id, settings, enabled=..., role=..., expected_revision_id=...)` / `updatePluginConfig(pluginId, settings, { enabled, role, expectedRevisionId })` send only the fields you give.

### For MCP

- **Streamable HTTP** is the network transport for the Node and Python servers. Only the Python server keeps the older SSE transport, for clients that need it; the Node SSE server is gone.
- **Each caller uses its own Faxbot key**, sent as `Authorization: Bearer` or `X-API-Key`, including the servers built into the API. The installation key is never used for network MCP requests. Only stdio uses `API_KEY`.
- **Settings from the environment.** `MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS`, `MCP_OAUTH_SUBJECT_KEYS_FILE` (maps OAuth token subjects to Faxbot keys) and `MCP_RESOURCE_URL` (OAuth protected-resource metadata) are read from the process environment, also for the built-in servers. See [MCP transports](mcp/transports.md).

### Upgrade

1. Stop every Faxbot API process that uses the installation: all workers and all containers.
2. Back up. An installation from an earlier release has no installation key yet, so copy its database and data folder yourself.
3. Run `faxbot system migrate` with the new version. An upgraded installation that never set `FAX_BACKEND` keeps using Phaxio; a new installation starts with no provider until you choose one.
4. Start Faxbot, sign in with the installation key, and create the first owner.
5. Approve or replace keys waiting for review, and issue new keys for apps that open received documents.
6. Make a fresh backup with `faxbot system backup`.

The steps, with Docker commands, are in [Upgrade an installation](deployment.md#upgrade-an-installation).
