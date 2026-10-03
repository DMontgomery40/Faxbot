<div class="home-hero" align="center" markdown>
<img src="assets/images/faxbot_full_logo_banner.png" alt="Faxbot" style="max-width: 980px; width: 100%; height: auto;" />

Open‑source, self‑hostable fax API with modular backends, a mobile‑ready Admin Console, and AI assistant tooling.

[:material-rocket-launch: Get Started](getting-started.md){ .md-button .md-button--primary }
[:material-monitor-dashboard: Admin Console](admin-console.md){ .md-button }
[:material-shield-lock: Security](security/index.md){ .md-button }
[:material-play: Admin Demo](admin-demo.md){ .md-button }

<br/>

<div class="grid cards" markdown>

- :material-rocket-launch: **Getting Started**  
  Launch locally and run the Setup Wizard.  
  [:octicons-arrow-right-24: Guide](getting-started.md)

- :material-cog: **Admin Console**  
  Configure providers, auth, storage; see Diagnostics.  
  [Open](admin-console.md)

- :material-cloud-lock: **Deployment**  
  Checklist before exposing your API publicly.  
  [Read](deployment.md)

- :material-shield-lock: **Security**  
  Auth, OAuth/OIDC, HIPAA notes.  
  [Docs](security/index.md)

- :material-api: **API & SDKs**  
  REST reference and client libraries.  
  [API](api.md) · [SDKs](sdks/index.md)

- :material-robot-excited: **MCP (AI)**  
  Node and Python servers + transports.  
  [Learn](mcp/index.md)

</div>
</div>

## Backends & Operations

<div class="grid cards" markdown>

- :material-fax: **Provider Playbooks**  
  Go‑Live overview and backend specifics.  
  [Overview](go-live/index.md) · [Phaxio](go-live/phaxio.md) · [Sinch](go-live/sinch.md) · [SIP/Asterisk](go-live/sip-asterisk.md)

- :material-inbox-arrow-down: **Inbound & Storage**  
  Enable inbound, choose storage, token TTLs.  
  [Inbound](inbound.md)

- :material-image: **Images & PDFs**  
  File types, limits, and conversion tips.  
  [Guide](guides/images-and-pdfs.md)

- :material-lan: **Networking & Tunnels**  
  Options for public access during evaluation.  
  [Guide](networking/tunnels.md)

- :material-stethoscope: **Troubleshooting**  
  Common errors and quick fixes.  
  [Checklist](troubleshooting.md)

- :material-monitor-account: **Admin Demo**  
  Hosted, simulated console (no providers).  
  [Try it](admin-demo.md)

</div>

## Build & Integrate

Faxbot's API runs on your self-hosted installation. The `faxbot.net` website, simulated demo and static API-reference pages are separate from your server.

- REST API reference: [Generated source reference](generated/index.md) and [API guide](api.md)
- Swagger UI: open `/docs` on your installation, for example `http://localhost:8080/docs` in local development
- SDK docs: [Overview](sdks/index.md), [Node](sdks/node.md), [Python](sdks/python.md)
- AI workflows: [MCP integration](mcp/index.md) with Node and Python servers

## Need Help?

- Use the hosted [Admin Demo](admin-demo.md) for a guided tour
- Check [Security](security/index.md) for HIPAA and OAuth requirements
- Open an issue and mention your active backend so we can target guidance quickly
