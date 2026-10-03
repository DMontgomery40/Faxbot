# Authentication

## Refresh implementation status

This branch includes persistent authentication endpoints under `/auth` and current
permission checks for outbound submission and operator fax views. The console and
remaining installation, inbound and client adapters are tracked in the [repository access-control plan](https://github.com/DMontgomery40/Faxbot/blob/feat/faxbot-refresh/docs/superpowers/plans/2026-10-02-faxbot-access-control.md).
The new session endpoints alone do not establish complete RBAC for the installation.

The authentication API provides password login, key-to-session login, current
identity, logout, password replacement, session listing and session revocation.
Use the generated OpenAPI reference for the exact request models. Sessions expire
after 12 hours, or 30 minutes without use, and cannot outlive their source key.
Revoked, disabled or changed sources are checked against current installation state.

### Browser and client transport

Authentication and the converted outbound endpoints require HTTPS for remote clients, including
header-only API-key clients. Browser requests must use a trusted console origin.
Set deployment `FAXBOT_CONSOLE_ORIGINS` to a comma-separated list of origins, such
as `https://fax.internal.example,https://fax.example:8443`. Entries contain a scheme,
host and optional port, without paths. If omitted, the active `PUBLIC_API_URL`
origin is used. Configure an override when the console host differs from the
provider callback host. Only trust proxy headers from the actual reverse proxy.

For local development, run `scripts/run-uvicorn-dev.sh`, or use the installed
runtime from the repository root:

```sh
python -m api.app.server --loopback --port 8080
```

This explicit development launcher binds `127.0.0.1`, disables proxy rewriting,
and enables the loopback transport profile. Setting
`FAXBOT_ALLOW_INSECURE_LOOPBACK=true` on an ordinary Uvicorn process is insufficient.
The launcher supplies localhost/127.0.0.1 origins for its port unless an explicit
origin list is configured. It does not print or generate a bootstrap key.

HTTPS sessions use an HttpOnly, Secure, SameSite=Strict, host-only cookie. Local
development uses a separate cookie. Login requires an allowed browser Origin;
cookie-authenticated changes additionally require `X-CSRF-Token` from `/auth/me`.
Explicit `X-API-Key` takes precedence over a cookie, including when the supplied
key is invalid. API keys and session tokens do not belong in URLs or browser
persistent storage. Authentication responses are marked `no-store`.

### Outbound permissions

Submission requires current `fax:send` access to the authenticated principal's own
personal container. Acceptance records that resource with the fax and its captured
provider account in one transaction. Rotating or revoking a human credential does
not requeue or cancel an already accepted provider attempt.

Job lists, filtered totals, detail and delivery history require `fax:read` on the
individual resources. Visibility is applied before counting and pagination. A
retained document requires the independent `fax:document` permission; document
access does not grant metadata access. Refresh additionally requires `fax:refresh`,
and receipt reconciliation requires `fax:reconcile` plus metadata access.

Request replay preserves the credential's stable namespace. A replay still needs
current send permission and permission to read the original fax; knowing an old
idempotency key is not authority. A revoked or disabled source returns401. Hidden
and missing resources return404; a visible resource with a denied action returns403.

`MAX_REQUESTS_PER_MINUTE` retains the optional send/status request limit. Password
and database-key verification also use the installation's bounded authentication
admission service. Throttled responses provide `Retry-After`.

### Integration boundaries

The remaining legacy key-management routes are not the completed named-user and
scoped-key management contract. Their free-text owner labels and historical scopes
must not be interpreted as new role assignments or unrestricted console authority.
The console login migration, inbound permissions, provider-fetch capabilities,
terminal access and retained client integration remain release requirements in the
repository plan. The converted endpoints do not permit unauthenticated access
merely because legacy `REQUIRE_API_KEY` is false.

The generated reference describes the actual registered request and response
models. Complete RBAC and production readiness require the remaining route and
client integration plus real browser and delivery verification; successful
authentication alone does not establish those results.
