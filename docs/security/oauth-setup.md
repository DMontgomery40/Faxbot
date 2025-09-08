---
layout: default
title: OAuth Setup
parent: Security
nav_order: 2
---

# OAuth2 / OIDC Setup for Faxbot MCP (SSE)

This guide shows how to configure JWT validation for the SSE transport. It covers the minimal concepts, the three env vars Faxbot needs, and quick links for popular identity providers.

<details markdown="1">
<summary>Environment variables</summary>

What the MCP SSE server validates
- Issuer (`iss`): matches `OAUTH_ISSUER`
- Audience (`aud`): matches `OAUTH_AUDIENCE` (e.g., `faxbot-mcp`)
- Signature: verified against the provider's JWKS (`OAUTH_JWKS_URL`)
- Expiry / not-before: standard JWT time checks

Environment variables
```
OAUTH_ISSUER     # Your OIDC issuer URL (no trailing slash)
OAUTH_AUDIENCE   # The audience/identifier of the API you expose to clients (string)
OAUTH_JWKS_URL   # JWKS endpoint (optional if your issuer uses the default well-known path)
```
</details>

General steps (any provider)
1) Create or identify an API/resource in your IdP.
   - Set an identifier value (this becomes the `aud` claim). Example: `faxbot-mcp`.
2) Create a client/app that can obtain access tokens for that API (client credentials or the flow used by your client).
3) Find your issuer URL and JWKS URL.
   - The issuer is the base of your OIDC realm/tenant.
   - The JWKS URL is advertised as `jwks_uri` in the OIDC discovery document at `${issuer}/.well-known/openid-configuration`.
4) Export env vars and start the SSE server (Node or Python).
5) Obtain a token from the IdP and connect to `/sse` with `Authorization: Bearer <token>`.

Provider specifics and links

Auth0
- Issuer: `https://YOUR_TENANT.auth0.com`
- Audience: your API Identifier (e.g., `faxbot-mcp`)
- JWKS: `https://YOUR_TENANT.auth0.com/.well-known/jwks.json`
- Docs:
  - Create API (audience): https://auth0.com/docs/get-started/apis/enable-api-authorization
  - JWKS and token validation: https://auth0.com/docs/secure/tokens/json-web-tokens/json-web-key-sets
  - Client credentials flow: https://auth0.com/docs/get-started/authentication-and-authorization-flow/client-credentials-flow

Okta
- Issuer: `https://YOUR_DOMAIN.okta.com/oauth2/default` (or your custom auth server)
- Audience: the custom API audience you configure
- JWKS: `${issuer}/v1/keys` (Okta uses `/v1/keys`, not the generic `/.well-known/jwks.json`)
- Docs:
  - Authorization servers & discovery: https://developer.okta.com/docs/guides/customize-authz-server/main/
  - Validate access tokens / JWKS: https://developer.okta.com/docs/guides/validate-access-tokens/main/

Microsoft Entra ID (Azure AD)
- Issuer: `https://login.microsoftonline.com/<TENANT_ID>/v2.0`
- Audience: App Registration → "Expose an API" → Application ID URI (or custom ID you set)
- JWKS: `https://login.microsoftonline.com/<TENANT_ID>/discovery/v2.0/keys`
- Docs:
  - OIDC discovery: https://learn.microsoft.com/azure/active-directory/develop/v2-protocols-oidc
  - App registration / Expose an API: https://learn.microsoft.com/azure/active-directory/develop/quickstart-configure-app-expose-web-apis

Google Identity (Workforce/Cloud)
- Issuer: `https://accounts.google.com`
- Audience: your audience string; ensure your token provider includes it in `aud`
- JWKS: `https://www.googleapis.com/oauth2/v3/certs`
- Docs:
  - OIDC discovery: https://accounts.google.com/.well-known/openid-configuration

Keycloak (self‑hosted)
- Issuer: `https://YOUR_HOST/realms/YOUR_REALM`
- Audience: client ID or custom audience claim (depends on realm configuration)
- JWKS: `${issuer}/protocol/openid-connect/certs`
- Docs:
  - OpenID Connect endpoints: https://www.keycloak.org/docs/latest/securing_apps/#openid-connect-endpoints

How to test quickly (Auth0 example)
```
# 1) Request a token using client credentials
export AUTH0_DOMAIN=YOUR_TENANT.auth0.com
export AUTH0_CLIENT_ID=...
export AUTH0_CLIENT_SECRET=...
export AUDIENCE=faxbot-mcp
TOKEN=$(curl -s https://$AUTH0_DOMAIN/oauth/token \
  -H 'content-type: application/json' \
  -d '{"grant_type":"client_credentials","client_id":"'"$AUTH0_CLIENT_ID"'","client_secret":"'"$AUTH0_CLIENT_SECRET"'","audience":"'"$AUDIENCE"'"}' | jq -r .access_token)

# 2) Connect to SSE (replace 3002 or 3003 depending on Node/Python container)
curl -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" http://localhost:3002/sse
```

Notes
- You may set `OAUTH_JWKS_URL` explicitly if your provider's JWKS path differs from the default (e.g., Okta's `/v1/keys`, Keycloak's `/protocol/openid-connect/certs`).
- The SSE servers do not mint tokens; they only validate them. Use your IdP or an internal OAuth server to issue client tokens.
- For HIPAA deployments, ensure your IdP and reverse proxy enforce TLS, MFA, and appropriate policies.
