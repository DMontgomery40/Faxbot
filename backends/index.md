---
layout: default
title: Backends
nav_order: 4
has_children: true
permalink: /backends/
---

<div class="home-hero">
  <img src="{{ site.baseurl }}/assets/images/faxbot_full_logo.png" alt="Faxbot logo" />
</div>

# Fax Backends

Faxbot supports multiple backends for sending faxes. Choose the one that best fits your needs:

## Backend Comparison

| Feature | Sinch (Direct) | Documo (mFax) | Phaxio | SIP/Asterisk |
|---------|-----------------|---------------|--------|--------------|
| **Setup Time** | 5 minutes | 5 minutes | 10–15 minutes | 1–2 hours |
| **Telephony Knowledge** | None | None | None | T.38/SIP expertise |
| **Domain/Tunnel Needed** | No | No | Often (for callbacks/fetch) | No |
| **Cost Model** | Pay per fax | Pay per fax | Pay per fax | SIP trunk only |
| **Control Level** | Provider-managed | Provider-managed | Provider-managed | Full control |
| **HIPAA Ready** | Yes (with BAA) | Check plan | Yes (with BAA) | Yes (self-hosted) |

## Recommendations

- **Most users**: Start with [Sinch Setup](sinch-setup.html) or [Documo Setup](documo-setup.html) — both work without a domain
- **High volume**: Consider [SIP/Asterisk Setup](sip-setup.html) for cost savings
- **Existing Sinch users**: Use [Sinch Setup](sinch-setup.html) if you already have an account
- **Prefer SignalWire**: Use [SignalWire Setup](signalwire-setup.html) for the Compatibility API flow
- **FreeSWITCH operators**: See [FreeSWITCH Setup](freeswitch-setup.html) for self‑hosted fax via mod_spandsp

{: .highlight }
All backends support the same API endpoints and features. You can switch between them by changing your configuration.
