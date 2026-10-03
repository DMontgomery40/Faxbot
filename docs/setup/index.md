# Setup

Pick your backend and environment, then deploy.

<div class="grid cards" markdown>

- :material-fax: **Phaxio (Cloud)**  
  Fastest path with HIPAA options.  
  [Guide](phaxio.md)

- :material-cloud: **Sinch (Cloud v3)**  
  Direct upload (“Phaxio by Sinch” accounts).  
  [Guide](sinch.md)

- :material-server: **SIP/Asterisk (Self‑hosted)**  
  Full control with your SIP trunk.  
  [Guide](sip-asterisk.md)

- :material-phone-in-talk: **SIP trunk (your carrier)**  
  Fax by the minute through Telnyx, SignalWire, Sinch, AnveoDirect, Flowroute or another carrier.  
  [Guide](sip-trunk.md)

- :material-cloud-lock: **Deployment**  
  Checklist for public exposure and TLS.  
  [Read](../deployment.md)

- :material-shield-lock: **Security**  
  Auth, OAuth/OIDC, HIPAA notes.  
  [Overview](../security/index.md)

- :material-lan: **Public Access & Tunnels**  
  Quick options for evaluation.  
  [Guide](public-access.md)

</div>

---

## What to choose
- Cloud: Phaxio or Sinch Fax API v3
- Self‑hosted: SIP/Asterisk (AMI + T.38)

## Checklist
- Backend selection and credentials
- Public URL and TLS (cloud backends)
- API key and scopes
- Storage and retention (inbound)

## Guides
- Backends (Go‑Live): [go-live/index.md](../go-live/index.md)
- Phaxio (Cloud): [phaxio.md](phaxio.md)
- Sinch (Cloud v3): [sinch.md](sinch.md)
- SIP/Asterisk (Self‑hosted): [sip-asterisk.md](sip-asterisk.md)
- SIP trunk with your own carrier: [sip-trunk.md](sip-trunk.md)
- Deployment: [../deployment.md](../deployment.md)
- Security overview: [../security/index.md](../security/index.md)
