# OAuth2 / OIDC Setup for Faxbot MCP (SSE)

This guide shows how to configure JWT validation for the SSE transport. It covers the minimal concepts, the three env vars Faxbot needs, and quick links for popular identity providers.

What the MCP SSE server validates
- Issuer (`iss`): matches `OAUTH_ISSUER`
- Audience (`aud`): matches `OAUTH_AUDIENCE` (e.g., `faxbot-mcp`)
- Signature: verified against the provider’s JWKS (`OAUTH_JWKS_URL`)
- Expiry / not-before: standard JWT time checks

## Environment variables

```env
OAUTH_ISSUER="https://YOUR_ISSUER"        # OIDC issuer URL (no trailing slash)
OAUTH_AUDIENCE="faxbot-mcp"               # Audience/identifier of the API exposed to clients
OAUTH_JWKS_URL="https://.../jwks.json"    # Optional, if not at the standard discovery path
```

## General steps (any provider)
1) Create or identify an API/resource in your IdP.  
   :material-target: Set an identifier value (this becomes the `aud` claim). Example: `faxbot-mcp`.
2) Create a client/app that can obtain access tokens for that API (client credentials or the flow used by your client).
3) Find your issuer URL and JWKS URL.  
   :material-web: The issuer is the base of your OIDC realm/tenant.  
   :material-key-chain: The JWKS URL is advertised as `jwks_uri` in the OIDC discovery document at `${issuer}/.well-known/openid-configuration`.
4) Export env vars and start the SSE server (Node or Python).
5) Obtain a token from the IdP and connect to `/sse` with `Authorization: Bearer <token>`.

## Provider specifics and links

### Auth0

:material-web: Issuer
: `https://YOUR_TENANT.auth0.com`

:material-target: Audience
: your API Identifier (e.g., `faxbot-mcp`)

:material-key-chain: JWKS
: `https://YOUR_TENANT.auth0.com/.well-known/jwks.json`

:material-book-open-page-variant: Docs
: - Create API (audience): https://auth0.com/docs/get-started/apis/enable-api-authorization  
  - JWKS and token validation: https://auth0.com/docs/secure/tokens/json-web-tokens/json-web-key-sets  
  - Client credentials flow: https://auth0.com/docs/get-started/authentication-and-authorization-flow/client-credentials-flow

:material-shield-account: Okta

:material-web: Issuer
: `https://YOUR_DOMAIN.okta.com/oauth2/default` (or your custom auth server)

:material-target: Audience
: the custom API audience you configure

:material-key-chain: JWKS
: ``${issuer}/v1/keys`` (Okta uses `/v1/keys`, not the generic `/.well-known/jwks.json`)

:material-book-open-page-variant: Docs
: - Authorization servers & discovery: https://developer.okta.com/docs/guides/customize-authz-server/main/  
  - Validate access tokens / JWKS: https://developer.okta.com/docs/guides/validate-access-tokens/main/

:material-microsoft-azure: Microsoft Entra ID (Azure AD)

:material-web: Issuer
: `https://login.microsoftonline.com/<TENANT_ID>/v2.0`

:material-target: Audience
: App Registration → “Expose an API” → Application ID URI (or a custom ID you set)

:material-key-chain: JWKS
: `https://login.microsoftonline.com/<TENANT_ID>/discovery/v2.0/keys`

:material-book-open-page-variant: Docs
: - OIDC discovery: https://learn.microsoft.com/azure/active-directory/develop/v2-protocols-oidc  
  - App registration / Expose an API: https://learn.microsoft.com/azure/active-directory/develop/quickstart-configure-app-expose-web-apis

:material-google: Google Identity (Workforce/Cloud)

:material-web: Issuer
: `https://accounts.google.com`

:material-target: Audience
: your audience string; ensure your token provider includes it in `aud`

:material-key-chain: JWKS
: `https://www.googleapis.com/oauth2/v3/certs`

:material-book-open-page-variant: Docs
: - OIDC discovery: https://accounts.google.com/.well-known/openid-configuration

:material-shield-lock: Keycloak (self‑hosted)

:material-web: Issuer
: `https://YOUR_HOST/realms/YOUR_REALM`

:material-target: Audience
: client ID or custom audience claim (depends on realm configuration)

:material-key-chain: JWKS
: ``${issuer}/protocol/openid-connect/certs``

:material-book-open-page-variant: Docs
: - OpenID Connect endpoints: https://www.keycloak.org/docs/latest/securing_apps/#openid-connect-endpoints

!!! tip
    JWKS paths vary by provider. If discovery doesn’t return the path you expect, set `OAUTH_JWKS_URL` explicitly (e.g., Okta `.../v1/keys`, Keycloak `/protocol/openid-connect/certs`).

## How to test quickly

=== "Auth0"

```bash
# 1) Request a token using client credentials
export AUTH0_DOMAIN=YOUR_TENANT.auth0.com
export AUTH0_CLIENT_ID=...
export AUTH0_CLIENT_SECRET=...
export AUDIENCE=faxbot-mcp
TOKEN=$(curl -s https://$AUTH0_DOMAIN/oauth/token \
  -H 'content-type: application/json' \
  -d '{"grant_type":"client_credentials","client_id":"'"$AUTH0_CLIENT_ID"'","client_secret":"'"$AUTH0_CLIENT_SECRET"'","audience":"'"$AUDIENCE"'"}' | jq -r .access_token)

# 2) Connect to the Python SSE server (port 3003)
curl -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" http://localhost:3003/sse
```

=== "Generic"

```bash
# Replace the token request with your IdP's client credentials endpoint,
# then supply the resulting access token when connecting to SSE
TOKEN="<your_access_token>"
curl -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" http://localhost:3003/sse
```

Notes
- You may set `OAUTH_JWKS_URL` explicitly if your provider’s JWKS path differs from the default (e.g., Okta’s `/v1/keys`, Keycloak’s `/protocol/openid-connect/certs`).
- The SSE servers do not mint tokens; they only validate them. Use your IdP or an internal OAuth server to issue client tokens.
- For HIPAA deployments, ensure your IdP and reverse proxy enforce TLS, MFA, and appropriate policies.
