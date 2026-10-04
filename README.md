<p align="center">
  <img src="assets/faxbot_full_logo.png" alt="Faxbot logo" width="100%" />
</p>
<p align="center">
  <a href="https://dmontgomery40.github.io/Faxbot/">
    <img alt="View the Documentation" src="https://img.shields.io/badge/Docs-Faxbot-2b5fff?style=for-the-badge">
  </a>
</p>

<p align="center">
  <a href="https://faxbot.net">
    <img alt="Visit the Website" src="https://img.shields.io/badge/Website-faxbot.net-00c853?style=for-the-badge">
  </a>
</p>

<p align="center">
  <a href="https://faxbot.net/admin-demo">
    <img alt="UI Demo" src="https://img.shields.io/badge/UI%20Demo-Admin%20Console-ff9800?style=for-the-badge">
  </a>
</p>


Faxbot is an open-source, self-hosted fax platform with a web Admin Console, a REST API, cloud and SIP providers, and integrations for AI assistants. It helps organizations send and receive documents, choose delivery routes, track costs, and deliver incoming documents to the inboxes staff already use.

This README describes the current source checkout. Published packages and deployed installations may have fewer features; the [roadmap](#roadmap) distinguishes implemented capabilities from planned work and experiments.

## What Faxbot does

- **Send and receive faxes.** Prepare PDF, TXT, and supported TIFF documents, track outbound jobs, and organize received faxes in mailboxes. Uncertain submissions wait for confirmation instead of being blindly sent again. A received fax shows as waiting until Faxbot has fetched and checked its real document; see [receiving faxes](docs/operations/receiving.md).
- **Choose providers independently.** Use separate outbound and inbound providers. Built-in adapters cover Phaxio, Sinch, Documo, HumbleFax, eFax, SignalWire, SIP/Asterisk, and FreeSWITCH; supported operations vary by provider. Additional HTTP providers can use manifests. See [provider setup](docs/setup/index.md) and [the plugin registry](docs/plugins/registry.md).
- **Fax through your own SIP carrier or phone system.** Connect a carrier SIP trunk (Telnyx first; SignalWire, Sinch, AnveoDirect, Flowroute, Gamma and BT One Voice in the UK, Telstra SIP Connect in Australia, or another) to the built-in Asterisk engine, or fax through your office's Avaya IP Office or Aura phone system on the local network. Fax over T.38 by the minute with no per-page fee, and keep a record of every call. See [SIP trunk setup](docs/setup/sip-trunk.md) and [Avaya IP Office and Aura](docs/setup/avaya.md).
- **Choose delivery routes and track spending.** Configure additional outbound routes, rate cards (including flat monthly plans), and destination preferences. Faxbot uses price estimates and delivery history to rank routes, records each attempt, and reconciles reported SignalWire charges and, with a `TELNYX_API_KEY`, what Telnyx billed for each sent and received trunk call, separately from delivery status. Unknown charges stay unknown and a route with an unknown cost is never called the cheapest. See [delivery routes](docs/operations/delivery-routes.md).
- **Deliver incoming documents by email.** One intake queue collects ordinary faxes and direct deliveries. SMTP connectors send the received PDF to configured inboxes, with retries for confirmed temporary failures and review for uncertain outcomes. See [intake](docs/operations/intake.md).
- **Hand received documents to an owner.** The **Work** screen and `faxbot received owners` give each received document an owner who acknowledges it and marks it done, with an optional operational acknowledgement target per installation or mailbox, one escalation to a backup person, and a per-item evidence export. An owner must already be able to see the document; assignment never grants access. See [Work](docs/operations/work.md).
- **Send directly to verified Faxbot partners.** Enrolled installations can exchange encrypted original PDFs and signed receipts using the recipient's usual fax number. Enrollment requires both installations and a fax challenge; ordinary fax fallback preserves the delivery's identity and uncertainty checks. See [direct delivery](docs/operations/direct-delivery.md).
- **Send short faxes to one number together.** When a recipient agrees, faxes to its number can wait a few minutes and share one SIP trunk call, which saves per-call minimums. See [sending together](docs/operations/delivery-routes.md#sending-together).
- **Avoid repeating accepted case documents.** The case-packet API can send an index and only new or changed documents when an administrator records that the recipient accepts references. Faxbot checks that setting; collecting evidence of consent is future work. Preview packets before sending. See [case packets](docs/operations/delivery-routes.md#case-packets).
- **Manage people and integrations.** Named users, groups, roles, mailbox permissions, scoped API keys, sessions, and mobile pairing are managed in the console. The server enforces the same permissions. See [access control](docs/security/access-control.md).

## Admin Console

The API serves the console at `/admin/ui/`. Configure providers, users, keys, mailboxes, storage, and diagnostics there. **Tools → Delivery routes** shows spending, destinations, rate cards, and direct partners; **Tools → Intake** manages incoming documents and email delivery.

Screens follow the signed-in user's permissions and provider capabilities. See the [console guide](docs/admin-console.md) and [Setup Wizard](docs/admin-console/setup-wizard.md).

## Quick start with Docker Compose

1. Copy the bootstrap configuration:

   ```sh
   cp .env.example .env
   ```

2. Replace the example credentials, set a strong installation `API_KEY`, and choose a provider using its [setup guide](docs/setup/index.md). For an initial document-only evaluation, set `FAX_DISABLED=true` before the first startup: jobs remain held and are not reported as successful transmissions.
3. Set the public URL and console origins for your deployment. Console sessions require HTTPS. For an HTTP-only Docker evaluation on a trusted private network, explicitly set `FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS=true`; see [authentication](docs/security/authentication.md#https-and-plain-http) for the transport requirements.
4. Build and start the API and console:

   ```sh
   docker compose up -d --build api
   ```

   SIP/Asterisk installations also need the `asterisk` service and a configured trunk; follow the [Asterisk guide](docs/setup/sip-asterisk.md).

5. Open `http://localhost:8080/admin/ui/` for a local HTTP evaluation, or `/admin/ui/` on your configured HTTPS address. Sign in with the installation key to [create the first owner](docs/security/access-control.md#create-the-first-owner), then use a named account for daily work.

Health checks are available at `/health` and `/health/ready`. Readiness does not prove that a fax has been delivered. Faxbot starts even when it cannot sign in to its fax engine (Asterisk): readiness, the dashboard and Settings give the reason in one sentence, sends are refused with it, and Faxbot keeps trying. The Compose file restarts the `api` and `asterisk` services after any exit, including **Restart API** in the console.

Credentials in `.env` (provider keys, passwords and secrets) are read at every start and are the values in force; the console shows them as **Set in .env** and they are changed there, followed by `docker compose up -d` (a plain `docker compose restart` keeps the old values). `API_KEY` (the installation key) is read only at the first start, and other settings are managed in the console after that. Follow any requested restart (Settings offers **Restart now**) and confirm the active settings before transmitting. [Held test jobs](docs/setup/test-mode.md) remain held when sending is enabled later.

## SDKs and AI assistants

Use the [Python SDK](docs/sdks/python.md) or [Node.js SDK](docs/sdks/node.md) to call Faxbot's API:

```sh
pip install faxbot
npm install faxbot
```

Another system can hand a PDF to the work queue with `POST /imports` and a small JSON manifest; see [import documents](docs/operations/work.md#import-documents-from-another-system).

The Node and Python MCP servers provide fax tools for AI assistants over stdio and Streamable HTTP; Python also supports legacy SSE. Remote requests use the caller's own Faxbot identity. See [MCP setup](docs/mcp/index.md) and [transport authentication](docs/mcp/transports.md).

## Command line

The `faxbot` command does what the Admin Console does, apart from its built-in terminal, under the same eight areas: after `faxbot send` and `faxbot status` come `received`, `sent`, `numbers`, `recipients`, `providers`, `costs`, `access` and `system`. Every setting can be read and changed with `faxbot system settings`. Output is readable by default and `--json` for scripts. Older command names such as `faxbot jobs list` keep working.

```
docker compose exec api faxbot system health
FAXBOT_API_KEY=... faxbot send +15551234567 referral.pdf
```

With Faxbot stopped, `faxbot system recover-owner`, `backup`, `restore` and `migrate` recover owner access, back up and restore an installation, and upgrade its database. See [Command line](docs/operations/cli.md) and the [command reference](docs/reference/cli.md).

## Mobile and desktop

The iOS companion connects to your Faxbot server to send faxes and check status. Pair it with a short-lived, single-use code from the console. See the [iOS guide](docs/apps/ios.md) for setup and TestFlight invitations, and the [desktop notes](ELECTRON_DESKTOP_APPS.md) for the Electron app.

![iOS Send Screen](assets/ios_send_screenshot.png)

## Documentation and development

- [Release notes](docs/release-notes.md), including the [upgrade steps](docs/deployment.md#upgrade-an-installation).
- [Getting started](docs/getting-started.md), [deployment](docs/deployment.md), and [provider setup](docs/setup/index.md).
- [API reference](docs/api.md), [access and sign-in API](docs/reference/access-api.md), and [reference overview](docs/reference/index.md).
- [Authentication](docs/security/authentication.md), [access control](docs/security/access-control.md), and [security](docs/security/index.md).
- [Contributing and local checks](CONTRIBUTING.md) and [agent instructions](AGENTS.md).
- [Enterprise correspondence architecture](planning/enterprise-correspondence.md): proposed shared workflows, configurable requirements and guided setup, with current-code gaps and acceptance criteria.

Contributions should keep the console usable, preserve provider capability checks, and update the README, roadmap, and affected operator documentation in the same change as the capability.

Reference pages under `docs/generated/` are generated from code. The README/roadmap, agent instructions and [planning sources](planning/README.md) are maintained separately. AI prose proposals are restricted to permitted documentation pages, including when applied locally.

## Roadmap

Checked items are implemented in the current source checkout. Unchecked items are planned or require validation; they are not claims about a release date or measured savings.

### Implemented

- [x] Durable outbound jobs, uncertain-outcome handling, server-side idempotency, and held test jobs.
- [x] Users, groups, roles, scoped keys, sessions, mailbox access, and single-use mobile pairing.
- [x] Cost-aware route selection, versioned rate cards, attempt costs, and separate reconciliation of reported SignalWire charges.
- [x] Telnyx charges for every trunk call, sent and received: matched by SIP Call-ID or by a unique number-and-time match (ambiguous records stay unmatched and are counted), kept with corrections and never changing delivery; shown per route in Spending, in Job Details and in the Inbox, and with `faxbot costs spending`, `reconcile` and `fax`. Calls Telnyx billed that Faxbot has no record of (such as a received fax whose hand-over failed) count in spending on their own line and attach to the received fax when exactly one matches. Route recommendations never call an unknown cost the cheapest; a flat plan reads "Included in your HumbleFax plan ($10 a month)." and counts its fee once per 30 days. Each provider in use gets its published price (Phaxio, Sinch, SignalWire, HumbleFax, SIP trunk carriers; read 2026-10-03) as an editable rate card on start, and the Dashboard reads the same spending as Tools. (Live on 3 October 2026: the sent calls' $0.01 and $0.005 and a received call's $0.0032 showed against the right faxes. Matching by the captured SIP Call-ID and the unrecorded-call sweep are tested with synthetic records only.)
- [x] Shared intake queue and SMTP email delivery, with console management.
- [x] Enrolled direct partners, encrypted original-PDF delivery, signed receipts, and controlled fax fallback.
- [x] Recipient-approved case packets, accepted-document history, and preview, in the console (**Recipients → Case packets**, with the recent cases listed) and through the API (`GET /cases`).
- [x] **Costs → Savings**: calls saved by sending together, fax calls avoided by direct delivery and pages case packets did not send again, each marked an estimate, from `GET /routing/savings` (`faxbot costs savings` is next). Case packets count from when Faxbot started recording what each packet left out. Route prices are worded in each card's own unit ("about $0.005 a minute, at least 1 minute"), and the Send preview estimates the document being sent ("About $0.01 for this 2-page fax"). **Costs → Recommendations** says what will appear there.
- [x] Sending together: short faxes to the same number share one SIP trunk call. It is off unless an operator records that the recipient agreed. A number holds faxes only on a trunk billed per call or with a minimum. Each number has a longest wait (default 10 minutes), a page cap (default 30) and a choice of whether different senders may share a call. **Send now** sends at once and takes the waiting faxes with it. Each document follows a separator page. Each fax keeps its own result, mapped from the call's confirmed pages, and nothing is sent again automatically. Job Details shows each fax's share of the charge; the number's Details shows calls saved and an estimate of money saved. Available in the console, the API and `faxbot recipients together`. Tested with synthetic faxes and over a local T.38 test line (two faxes in one call, and a call cut after the first fax); a live combined call is pending. See [sending together](docs/operations/delivery-routes.md#sending-together).
- [x] Carrier SIP trunk presets with T.38, per-call records, and native faxes priced by the trunk carrier. Proven live over Telnyx on 3 October 2026: a two-page fax sent to a cloud fax line and a fax received back, both configured entirely in the console.
- [x] Fax over a SIP trunk from behind a router with no published or forwarded ports: Asterisk registers and starts every flow itself, with audio fax for new calls one click away when T.38 data cannot come back. Proven live in both directions from behind a home router in audio mode; over Telnyx, T.38 data did not come back through a router that changes port numbers.
- [x] Encrypted trunk registration by default, Faxbot's internet address found by STUN, and one plain sentence per trunk call (for example "no fax data came back from the carrier") in Recent calls, Jobs, the Dashboard and `faxbot providers trunk`.
- [x] `faxbot` command line covering the product, with stopped-server owner recovery, backup, restore, and database upgrades.
- [x] The command line follows the console's eight areas: `faxbot send` and `status`, then `received`, `sent`, `numbers`, `recipients`, `providers`, `costs`, `access` and `system`, with plain help. Every older command keeps working, hidden from help, and the [reference](docs/reference/cli.md) lists each one's new name. `faxbot system settings set` accepts every setting by name. Tests fail when a setting or an operator route is missing from the console or the command line. `faxbot received counts`, `providers efax status`, `system settings reload`, `recipients together check`, `costs received` and `costs plans --in-use` read the same as the console; `sent evidence` and `sent confirm-receipt` show and settle an uncertain fax without sending it again; the `received` commands take either a received fax's ID or its owners-list ID.
- [x] How often Faxbot checks its internet address, the S3 bucket check in Diagnostics, the address phones use on your network and the documentation address are settings, changeable without a restart; an installation that set them in `.env` keeps its values. Each has its place in the console (the trunk page, System → Diagnostics, Access → Keys & phones, System → Developer), as do the Telnyx charges key, console restarts, the plugin switches (System → Developer → Provider plugins) and the installation's time zone (the Setup wizard's first step).
- [x] **System** in plain words: **Audit log** (who did what, filtered by person and action), the database in Diagnostics (kind, reachable or not, what it holds, and a warning for a file outside the data folder), Logs with plain column names, and environment-only settings shown read-only where they matter ("Set in .env" or "Not set", never a secret's value; `GET /admin/settings` `deployment`). Settings only the owner may change are shown disabled with one sentence to everyone else (`owner_only`), and Diagnostics' two switches apply separately. The API & SDKs quickstart installs the SDKs' current version.
- [x] One E.164 destination per fax, read for the installation country (UK and US), stored on the job, with versioned idempotent replays.
- [x] No blank TXT page from a final line break; one-bit TIFF pages stay one-bit in generated PDFs.
- [x] Stable send-operation ids in both SDKs, both MCP servers and the console, with an explicit resume path and no automatic resend.
- [x] Source-derived reference documentation and scoped AI prose proposals, with maintained planning outside the generated tree.
- [x] Credentials in `.env` read at every start (carrier names such as `TELNYX_PASS` accepted), shown as **Set in .env**; a new installation starts with no fax provider until one is chosen.
- [x] Setup Wizard chooses one provider for sending and one for receiving, shows one section per provider in use (the SIP trunk whenever it sends or receives) and saves each step as you move on, with **Restart now** when a change waits for a restart. Every screen and `faxbot` call each provider by one name, such as **SIP trunk (Asterisk)**.
- [x] **Apply and connect** (and `faxbot providers trunk apply`) writes the SIP trunk, restarts the Compose install's Asterisk to load it once no call is up, waits, and shows the trunk check on the same screen; no host shell is needed (tested with a simulated Asterisk manager; first live restart pending).
- [x] The Setup Wizard ends with an optional test: one test page sent only when asked and followed live to its outcome sentence and carrier cost, and a live wait for a fax sent to the installation's number (tested with simulated faxes; live test pending).
- [x] Audio fax chosen by Faxbot when T.38 cannot work: after a T.38 call gets no fax data back, or for a new Telnyx trunk on a network that changes port numbers, new calls use audio fax and the switch says why, with **Try T.38 again** (the failed fax is never resent). The fax marker on outgoing calls is on by default, and readiness waits until a SIP trunk is set up for each direction that uses it.
- [x] No hand-made shared secrets between Faxbot's containers: Faxbot creates the Asterisk manager password when the SIP trunk first comes into use and Asterisk picks it up by itself (a password in `.env` still wins), and the Setup Wizard says whether received faxes reach Faxbot instead of asking for the inbound secret (start-script and startup tests; live first boot pending).
- [x] A fax received over the SIP trunk always reaches the Inbox: Faxbot creates the Asterisk inbound secret itself, a failed hand-over is logged and named in Recent calls, **Check trunk status** and the Dashboard, and an image that was never handed over is brought in automatically or with `faxbot received recover` (tested with synthetic images; recovery of a live orphan pending).
- [x] Avaya IP Office and Aura as **Your phone system**: Faxbot connects as a SIP trunk on the local network. The phone system and Faxbot recognise each other by address, and the codec order follows the installation country. Numbers are dialled as a phone there dials them, with an outside-line prefix. The phone system override (`docker-compose.phone-system.yml`) publishes SIP and a small media range (default 20 ports; Asterisk refuses more than 100) on `FAXBOT_LAN_ADDRESS` only, and the console says what to give the administrator, with Avaya's checklist and sources. Tested with exact trunk files and a loopback proof: T.38 and audio faxes both ways to a stand-in phone system behind a simulated Docker host. Live Avaya sign-off pending.
- [x] UK and Australian carrier presets: Gamma, BT One Voice (new trunks start with audio fax because BT turns T.38 into audio) and Telstra SIP Connect, with the UK switch-off and BT's own fax statements in the setup guide. None publishes a price, so their rate cards say so. Tested against Faxbot's own Asterisk only; live BT, Gamma and Telstra sign-off pending.
- [x] eFax (US, UK and Australia) through the eFax Enterprise API: sending with eFax's status, receiving by asking eFax for new faxes (each fax recorded before it is downloaded, resumed after a restart, and deleted from eFax only when turned on, with refused deletions retried for seven days; an optional signed eFax notification makes Faxbot check eFax at once and its contents are never used), keys in `.env`, an eFax section in the Setup Wizard and Settings, `faxbot system settings validate efax`, and eFax's published plans with source and date (`faxbot costs plans efax`; the API itself is priced by quote). Built from eFax's published API specification and tested against a fake; not yet run against a real eFax account. See [eFax setup](docs/setup/efax.md).

### Next

- [ ] Live confirmation of SIP Call-ID matching in both directions and of the hourly check for calls Faxbot has no record of; charges from other trunk carriers.
- [ ] Live eFax sign-off with a real eFax Corporate API account.
- [ ] Live combined call over a real trunk for sending together, and live automatic audio switching after a failed T.38 call.
- [ ] Live sign-off of the Avaya IP Office, Avaya Aura, BT One Voice, Gamma and Telstra SIP Connect presets on real systems.
- [ ] SSLFax through an optional HylaFAX+ engine for peers that already support it, with normal-fax fallback; the [isolated experiment](docs/operations/delivery-routes.md#sslfax) is the starting point.

### Proposed enterprise foundation

Future work will extend Faxbot from document delivery into accountable correspondence across industries. Build on existing access controls and delivery infrastructure, retaining separate inbound and outbound providers. The [enterprise architecture](planning/enterprise-correspondence.md) maps implemented foundations, partial capabilities and proposed interfaces. This direction does not expand Phase 1 or the four fixes above.

- [x] **E0 — Trustworthy acquisition:** a received fax is recorded only from a checked notification and waits until its real document has arrived. It resumes after a restart or a repeated notification, and keeps its provider fax ID, account, receipt time and document digest. See [receiving faxes](docs/operations/receiving.md).
- [x] **E1 — Smallest useful workflow:** one generic import (`POST /imports`, `faxbot received import`), a mailbox-scoped owned queue (the **Work** screen, `faxbot received owners`), operational acknowledgement targets with one escalation to a backup person, and a permission-scoped evidence export. Transport delivery, email delivery and acknowledgement remain separate events. Tested with synthetic documents only; not validated against a customer system. See [Work](docs/operations/work.md).
- [ ] **E2 — Protected workflows:** a shared versioned policy foundation, exact-action approvals, verified recipient/purpose/channel policies, configurable retention and legal holds across managed copies.
- [ ] **E3 — Templates and guided setup:** optional, source-backed jurisdiction/industry templates; organization, mailbox and workflow settings; explained suggestions and visible missing requirements. Prioritize UK, Australian and US healthcare and other regulated workflows without shipping unverified legal defaults.
- [ ] **E4 — Enterprise integrations:** workforce identity and provisioning, controlled OCR/AI processing and case-system connectors that preserve evidence and reconcile uncertain outcomes.

Any control required by a selected real workflow is a prerequisite to using its records, regardless of stage number. Templates must state their scope, version, required settings, evidence and remaining organizational responsibilities. Customer-specific forms and integrations stay optional; these capabilities are not yet implemented as an enterprise workflow.

Enterprise software acceptance uses synthetic/local tests. Live customer end-to-end validation is separate and never a CI, merge or release requirement; see the [testing boundary](CONTRIBUTING.md#enterprise-testing-boundary).

### Later and experimental

- [ ] Destination-level scheduling and verified same-installation delivery, guided by actual traffic and retry costs.
- [ ] Additional intake connectors, including watched folders and email ingestion, through the proposed enterprise import contract; specific vendor integrations follow demonstrated needs.
- [ ] Measure fax negotiation, lossless compression, and error-correction choices before enabling adaptive transport behavior.
- [ ] Cost per delivered fax for each destination, from every attempt's negotiated speed, error correction, duration and real charge, and route choice based on it rather than on rate cards alone.
- [ ] Notice fax for enrolled partners whose intake needs a fax event: the original goes by the encrypted direct route and one opaque notice page goes by fax.
- [ ] Receiving-side savings from real call history: which numbers should share a channel pool and which stay metered, quiet numbers, and trunk consolidation.
- [ ] Resend only the missing pages to an enrolled partner after a broken call, with the document assembled whole on the receiving side.
- [ ] Assemble a packet from a recipient's own checklist, and send once to an organization that distributes internally, only where the recipient agrees.
- [ ] Reuse templates and unchanged pages between enrolled partners by content fingerprint, sending only what the other side does not already hold.
- [ ] Prove T.38 Internet Aware Fax interoperability on compatible endpoints before offering it as a transport.
- [ ] Evaluate SIP routing preferences, inbound channel pooling, and existing plan entitlements against real account costs and delivery reliability.
- [ ] Explore automatic partner discovery only with a verified number, organization, and inbox binding.
