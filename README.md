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

- **Send and receive faxes.** Prepare PDF, TXT, and supported TIFF documents, track outbound jobs, and organize received faxes in mailboxes. Uncertain submissions wait for confirmation instead of being blindly sent again.
- **Choose providers independently.** Use separate outbound and inbound providers. Built-in adapters cover Phaxio, Sinch, Documo, HumbleFax, SignalWire, SIP/Asterisk, and FreeSWITCH; supported operations vary by provider. Additional HTTP providers can use manifests. See [provider setup](docs/setup/index.md) and [the plugin registry](docs/plugins/registry.md).
- **Fax through your own SIP carrier.** Connect a carrier SIP trunk (Telnyx first; SignalWire, Sinch, AnveoDirect, Flowroute or another) to the built-in Asterisk engine, fax over T.38 by the minute with no per-page fee, and keep a record of every call. See [SIP trunk setup](docs/setup/sip-trunk.md).
- **Choose delivery routes and track spending.** Configure additional outbound routes, rate cards, and destination preferences. Faxbot uses price estimates and delivery history to rank routes, records each attempt, and reconciles reported SignalWire charges separately from delivery status. Unknown charges stay unknown. See [delivery routes](docs/operations/delivery-routes.md).
- **Deliver incoming documents by email.** One intake queue collects ordinary faxes and direct deliveries. SMTP connectors send the received PDF to configured inboxes, with retries for confirmed temporary failures and review for uncertain outcomes. See [intake](docs/operations/intake.md).
- **Send directly to verified Faxbot partners.** Enrolled installations can exchange encrypted original PDFs and signed receipts using the recipient's usual fax number. Enrollment requires both installations and a fax challenge; ordinary fax fallback preserves the delivery's identity and uncertainty checks. See [direct delivery](docs/operations/direct-delivery.md).
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

Health checks are available at `/health` and `/health/ready`. Readiness does not prove that a fax has been delivered.

After initial bootstrap, change server settings through the console. Editing `.env` does not replace saved configuration. Follow any requested restart and confirm the active settings before transmitting. [Held test jobs](docs/setup/test-mode.md) remain held when sending is enabled later.

## SDKs and AI assistants

Use the [Python SDK](docs/sdks/python.md) or [Node.js SDK](docs/sdks/node.md) to call Faxbot's API:

```sh
pip install faxbot
npm install faxbot
```

The Node and Python MCP servers provide fax tools for AI assistants over stdio and Streamable HTTP; Python also supports legacy SSE. Remote requests use the caller's own Faxbot identity. See [MCP setup](docs/mcp/index.md) and [transport authentication](docs/mcp/transports.md).

## Command line

The `faxbot` command does what the Admin Console does, apart from its built-in terminal: send and read faxes, manage users, groups, roles and API keys, change settings, check routes and costs, set up intake and direct delivery, and pair phones. Output is readable by default and `--json` for scripts.

```
docker compose exec api faxbot health
FAXBOT_API_KEY=... faxbot send +15551234567 referral.pdf
```

With Faxbot stopped, `faxbot admin` recovers owner access, backs up and restores an installation, and upgrades its database. See [Command line](docs/operations/cli.md) and the [command reference](docs/reference/cli.md).

## Mobile and desktop

The iOS companion connects to your Faxbot server to send faxes and check status. Pair it with a short-lived, single-use code from the console. See the [iOS guide](docs/apps/ios.md) for setup and TestFlight invitations, and the [desktop notes](ELECTRON_DESKTOP_APPS.md) for the Electron app.

![iOS Send Screen](assets/ios_send_screenshot.png)

## Documentation and development

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
- [x] Shared intake queue and SMTP email delivery, with console management.
- [x] Enrolled direct partners, encrypted original-PDF delivery, signed receipts, and controlled fax fallback.
- [x] Recipient-approved case packets, accepted-document history, and preview through the API.
- [x] Carrier SIP trunk presets with T.38, per-call records, and native faxes priced by the trunk carrier (proven in a loopback; live carrier call pending).
- [x] `faxbot` command line covering the product, with stopped-server owner recovery, backup, restore, and database upgrades.
- [x] One E.164 destination per fax, read for the installation country (UK and US), stored on the job, with versioned idempotent replays.
- [x] No blank TXT page from a final line break; one-bit TIFF pages stay one-bit in generated PDFs.
- [x] Stable send-operation ids in both SDKs, both MCP servers and the console, with an explicit resume path and no automatic resend.
- [x] Source-derived reference documentation and scoped AI prose proposals, with maintained planning outside the generated tree.

### Next


### Proposed enterprise foundation

Future work will extend Faxbot from document delivery into accountable correspondence across industries. Build on existing access controls and delivery infrastructure, retaining separate inbound and outbound providers. The [enterprise architecture](planning/enterprise-correspondence.md) maps implemented foundations, partial capabilities and proposed interfaces. This direction does not expand Phase 1 or the four fixes above.

- [ ] **E0 — Trustworthy acquisition:** recover incomplete imports, validate authentic artifacts and preserve source identity, receipt time and document provenance.
- [ ] **E1 — Smallest useful workflow:** one generic import, a mailbox-scoped owned queue, acknowledgement deadlines and an evidence export. Transport delivery and business acknowledgement remain separate.
- [ ] **E2 — Protected workflows:** a shared versioned policy foundation, exact-action approvals, verified recipient/purpose/channel policies, configurable retention and legal holds across managed copies.
- [ ] **E3 — Templates and guided setup:** optional, source-backed jurisdiction/industry templates; organization, mailbox and workflow settings; explained suggestions and visible missing requirements. Prioritize UK, Australian and US healthcare and other regulated workflows without shipping unverified legal defaults.
- [ ] **E4 — Enterprise integrations:** workforce identity and provisioning, controlled OCR/AI processing and case-system connectors that preserve evidence and reconcile uncertain outcomes.

Any control required by a selected real workflow is a prerequisite to using its records, regardless of stage number. Templates must state their scope, version, required settings, evidence and remaining organizational responsibilities. Customer-specific forms and integrations stay optional; these capabilities are not yet implemented as an enterprise workflow.

Enterprise software acceptance uses synthetic/local tests. Live customer end-to-end validation is separate and never a CI, merge or release requirement; see the [testing boundary](CONTRIBUTING.md#enterprise-testing-boundary).

### Later and experimental

- [ ] Destination-level scheduling and verified same-installation delivery, guided by actual traffic and retry costs.
- [ ] Additional intake connectors, including watched folders and email ingestion, through the proposed enterprise import contract; specific vendor integrations follow demonstrated needs.
- [ ] Measure fax negotiation, lossless compression, and error-correction choices before enabling adaptive transport behavior.
- [ ] Evaluate a supported SSLFax integration using the [isolated HylaFAX+ experiment](docs/operations/delivery-routes.md#sslfax); Faxbot does not currently offer SSLFax.
- [ ] Prove T.38 Internet Aware Fax interoperability on compatible endpoints before offering it as a transport.
- [ ] Evaluate SIP routing preferences, inbound channel pooling, and existing plan entitlements against real account costs and delivery reliability.
- [ ] Explore automatic partner discovery only with a verified number, organization, and inbox binding.
