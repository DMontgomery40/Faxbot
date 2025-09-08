---
layout: default
title: Security
nav_order: 5
has_children: true
permalink: /security/
---

<div class="home-hero">
  <img src="{{ site.baseurl }}/docs/assets/images/faxbot_full_logo.png" alt="Faxbot logo" />
</div>

# Security & Compliance

Faxbot is designed to handle sensitive healthcare data and can be configured for HIPAA compliance.

## Security Features

- **API Authentication**: X-API-Key header protection
- **HTTPS Enforcement**: TLS 1.2+ for all communications
- **Webhook Verification**: HMAC signature validation
- **OAuth2/JWT Support**: Enterprise-grade authentication for MCP
- **Audit Logging**: Comprehensive logging for compliance
- **PHI Protection**: Configurable data handling policies

## Compliance Considerations

### Healthcare Users (HIPAA Required)
- Must use secure backends with Business Associate Agreements (BAAs)
- HTTPS enforcement required
- Audit logging enabled
- Strong authentication mandatory

### Non-Healthcare Users
- Relaxed security settings available for convenience
- Optional authentication
- Reduced logging overhead
- HTTP allowed in development

{: .warning }
> This documentation provides technical guidance, not legal advice. Always consult your compliance team and legal counsel for HIPAA requirements.
