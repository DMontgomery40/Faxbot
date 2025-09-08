---
layout: default
title: Backends
nav_order: 3
has_children: true
permalink: /backends/
---

# Fax Backends

Faxbot supports multiple backends for sending faxes. Choose the one that best fits your needs:

## Backend Comparison

| Feature | Phaxio | Sinch | SIP/Asterisk |
|---------|--------|--------|--------------|
| **Setup Time** | 5 minutes | 10 minutes | 1-2 hours |
| **Telephony Knowledge** | None required | None required | T.38/SIP expertise |
| **Cost Model** | Pay per fax | Pay per fax | SIP trunk only |
| **Control Level** | Limited | Limited | Full control |
| **HIPAA Ready** | Yes (with BAA) | Yes (with BAA) | Yes (self-hosted) |

## Recommendations

- **Most users**: Start with [Phaxio Setup](phaxio-setup.html) - it's the easiest and most reliable
- **High volume**: Consider [SIP/Asterisk Setup](sip-setup.html) for cost savings
- **Existing Sinch users**: Use [Sinch Setup](sinch-setup.html) if you already have an account

All backends support the same API endpoints and features. You can switch between them by changing your configuration.
