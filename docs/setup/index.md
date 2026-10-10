# Setup

Choose your provider, then deploy.

A new installation starts with no fax provider. The console and `/health/ready` say "No fax provider set up yet." Sending is refused until you choose one in **Administration → Setup**. The Dashboard's **Set up a fax provider** button opens that page. An upgraded installation that never set `FAX_BACKEND` keeps using Phaxio.

<div class="grid cards" markdown>

- :material-fax: **Phaxio (Cloud)**  
  Fastest path with HIPAA options.  
  [Guide](phaxio.md)

- :material-cloud: **Sinch (Cloud v3)**  
  Direct upload (“Phaxio by Sinch” accounts).  
  [Guide](sinch.md)

- :material-cloud-upload: **Documo mFax (Cloud)**  
  Direct upload with status polling; no public document URL needed.  
  [Guide](documo.md)

- :material-cloud-upload-outline: **HumbleFax (Cloud)**  
  Direct upload for sending only; no public document URL needed.  
  [Guide](humblefax.md)

- :material-cloud-sync: **eFax (Cloud, US, UK and Australia)**  
  The eFax Enterprise API: sends, and receives by asking eFax, so no public address is needed.  
  [Guide](efax.md)

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
- Cloud: Phaxio, Sinch Fax API v3, SignalWire, Documo, HumbleFax (sends only) or eFax
- Self‑hosted: SIP/Asterisk (AMI + T.38)

## Checklist
- Backend selection and credentials
- Public URL and TLS (cloud backends)
- API key and scopes
- Storage and retention (inbound)

## Guides
- [Controlled Phaxio delivery check](../tools/phaxio-e2e-test.md)
- Phaxio (Cloud): [phaxio.md](phaxio.md)
- Sinch (Cloud v3): [sinch.md](sinch.md)
- SignalWire (Cloud): [signalwire.md](signalwire.md)
- Documo mFax (Cloud): [documo.md](documo.md)
- HumbleFax (Cloud, sending only): [humblefax.md](humblefax.md)
- eFax (Cloud, US, UK and Australia): [efax.md](efax.md)
- SIP/Asterisk (Self‑hosted): [sip-asterisk.md](sip-asterisk.md)
- SIP trunk with your own carrier: [sip-trunk.md](sip-trunk.md)
- Deployment: [../deployment.md](../deployment.md)
- Security overview: [../security/index.md](../security/index.md)
