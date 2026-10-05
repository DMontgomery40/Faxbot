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
- **Command names follow the console.** `faxbot` commands sit under the console's eight areas, and the earlier names are gone: `jobs` is now `sent`, `inbound` and `work` are `received`, `routing` is `recipients` and `costs`, `intake` is `received deliveries` and `numbers email`, `direct` and `cases` are under `recipients`, `trunk` is `providers trunk`, `settings`, `diagnostics`, `logs`, `health`, `restart`, `config` and `admin` are under `system` (`config` is `system profiles`), `users`, `integrations`, `groups`, `roles`, `keys`, `sessions`, `owner`, `resources`, `pair` and `me` are under `access` (`access grant` is `access grants add`), `mailboxes` is `numbers mailboxes`, `audit list` is `system audit`, `import` is `received import`, `providers config` is `providers show`, and `providers registry import` is `providers import`. See the [command reference](reference/cli.md).
- **Removed: remote-access tunnels, server checks and the plugin list.** The Remote access page and `faxbot system tunnel` set up nothing and are gone; reach Faxbot through your own domain or VPN. Phone pairing stays under **Access → Keys & phones**. The **Server checks** on Scripts & checks and `faxbot system actions` are gone; the Terminal and Diagnostics cover them. The provider list (`/plugin-registry`, `PLUGIN_REGISTRY_PATH`) is gone; a saved `PLUGIN_REGISTRY_PATH` is ignored for one release.

### For API clients

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
