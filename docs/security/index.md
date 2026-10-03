
# Security & Compliance

<div class="grid cards" markdown>

- :material-shield-key: **Authentication**  
  Console sign-in, sessions, API keys and HTTPS.  
  [Open](authentication.md)

- :material-account-key: **Access Control**  
  Users, groups, roles, mailboxes and keys.  
  [Open](access-control.md)

- :material-lock: **OAuth/OIDC Setup**  
  OAuth2/JWT for MCP SSE transports.  
  [Open](oauth-setup.md)

- :material-lan: **Network & Transports**  
  HTTPS enforcement, callback security, transport notes.  
  [Open](network.md)

- :material-hospital: **HIPAA Requirements**  
  Strict defaults and operational guidance.  
  [Read](../HIPAA_REQUIREMENTS.md)

</div>

Faxbot is designed to handle sensitive healthcare data and can be configured for HIPAA compliance.

## Security Features

- **Authentication**: every request needs an API key or a signed-in session
- **Access control**: roles, mailboxes and per-key permissions
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
- Reduced logging overhead
- HTTP allowed in development

{: .warning }
> This documentation provides technical guidance, not legal advice. Always consult your compliance team and legal counsel for HIPAA requirements.
