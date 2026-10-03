
# Diagnostics

Run environment checks and get targeted guidance.

Checks

- API health and version
- Backend configuration
  - Phaxio: API keys present, callback URL reachability hints, signature verification state
  - Sinch: project ID and credentials present; base URL sanity
  - SIP/Asterisk: AMI connectivity and authentication, Ghostscript availability for PDF→TIFF
- Public URL
  - `PUBLIC_API_URL` presence and HTTPS enforcement if enabled
- Storage
  - Installation-local path writable
  - Optional S3 access checks (when diagnostics enabled)
- Security posture
  - API key required, HTTPS enforced, audit logging enabled, file size limit

Actions

- “Restart API” (if allowed) exits one process. Pending activation requires every API worker to stop and the installation restart; arrange that through the process manager and verify active/desired identity afterward.
- Apply runtime fixes in canonical Settings using its loaded desired revision. Environment snippets are first-bootstrap/deployment references, not imports into an existing store.

If something fails

- Follow the actionable link beside the check (e.g., Backends, Security)
- See [Troubleshooting](../troubleshooting.md)

Related docs

- Backends: [Phaxio](../setup/phaxio.md), [Sinch](../setup/sinch.md), [SIP/Asterisk](../setup/sip-asterisk.md)
- Security: [Authentication](../security/authentication.md), [HIPAA](../HIPAA_REQUIREMENTS.md), [OAuth/OIDC](../security/oauth-setup.md)
- Deployment: [Guide](../deployment.md)
- Third‑Party: [Third-Party](../third-party.md)

## Under the Hood

- Readiness probe: `GET /health/ready`
- Admin health: `GET /admin/health-status` (aggregated checks for the console)
- DB status: `GET /admin/db-status`
- Logs: `GET /admin/logs` and `GET /admin/logs/tail`
- Restart: `POST /admin/restart` (requires `ADMIN_ALLOW_RESTART=true`)
