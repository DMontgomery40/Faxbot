# Authentication

Every request to the Faxbot API needs a credential: an API key in the `X-API-Key` header, or a signed-in admin console session. `REQUIRE_API_KEY=false` does not turn this off. The only routes that work without a credential are the ones that cannot carry one:

- health checks such as `GET /health`
- the console sign-in routes `POST /auth/login` and `POST /auth/key-login`
- provider callbacks and provider document downloads, which carry their own signature or single-job link
- `POST /mobile/pair`, which needs a current pairing code instead

Who can do what after signing in is covered in [Access Control](access-control.md). The exact request and response formats for the sign-in routes are in the [Access and Sign-in API](../reference/access-api.md) reference.

## Sign in to the admin console

The sign-in screen offers two ways in.

**Username and password.** People get a username from an Owner or Administrator under **Access → Users**, together with a temporary password. The first sign-in asks for a new password before anything else works. Passwords need at least 12 characters. Changing a password ends every other session for that person.

**API key.** Select **Sign in with API key** and paste a Faxbot API key. The console then works with exactly the permissions that key has. The session ends when the key expires or is revoked, even if the session's own time has not run out.

The installation key in `API_KEY` can also sign in this way. Use it to create the first owner and to recover owner access, then sign in with a named account for daily work. See [first owner](access-control.md#create-the-first-owner) and [owner recovery](access-control.md#recover-owner-access).

## Sessions

A console session lasts at most 12 hours. It also ends after 30 minutes without activity. Signing out, changing your password, or an administrator disabling your account or revoking the session ends it immediately.

Everyone can see and end their own sessions on **Settings → Sessions**. Seeing other people's sessions needs the `sessions:read` permission, and ending them needs `sessions:revoke`.

Faxbot keeps the session in a browser cookie that scripts cannot read. Over HTTPS the cookie is also marked secure, so the browser only sends it over HTTPS. API keys and session tokens never belong in URLs or in browser storage.

## Browser protections

Sign-in requests, and any change made with a session cookie, must come from a page Faxbot trusts. Faxbot compares the browser's `Origin` header with this list:

- `FAXBOT_CONSOLE_ORIGINS`, a comma-separated list of origins such as `https://fax.internal.example,https://fax.example:8443` (scheme, host and optional port, no paths)
- if that is not set, the origin of `PUBLIC_API_URL`

Set `FAXBOT_CONSOLE_ORIGINS` when people open the console at a different address than the one providers use for callbacks.

Changes made with a session cookie must also send the `X-CSRF-Token` header. The value comes from `GET /auth/me`, and the admin console sends it automatically. Requests that send `X-API-Key` do not use the cookie at all, even when the key is wrong.

## HTTPS and plain HTTP

Console sessions need HTTPS. There are two exceptions:

- **Local development.** `scripts/run-uvicorn-dev.sh`, or `python -m api.app.server --loopback --port 8080` from the repository root, listens only on `127.0.0.1` and allows sessions over plain HTTP from `http://localhost` and `http://127.0.0.1` on that port.
- **Private networks.** Setting `FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS=true` in the deployment environment allows sessions over plain HTTP. Use it only on a private network or VPN that you control, such as a WireGuard or Tailscale network. On any other network the session cookie can be read in transit. This is a deployment setting; it is not available on the Settings screen.

API keys work over any transport, HTTP or HTTPS, and from any client, including the desktop app. Outside a network you trust, use HTTPS or a VPN anyway: on plain HTTP the key travels unencrypted.

## API keys

API keys look like `fbk_live_<id>_<secret>`. The full key is shown once, when it is created or rotated. Every key belongs to a user or an integration and can never do more than its owner is allowed to do. A key can also carry a narrower permission list. See [Keys](access-control.md#keys) for creating, rotating and revoking keys, and for the keys that the iPhone app receives when it pairs.

Send the key in the `X-API-Key` header:

```sh
curl -H "X-API-Key: $FAXBOT_API_KEY" https://fax.example.com/fax/$JOB_ID
```

`MAX_REQUESTS_PER_MINUTE` limits how often one credential can send faxes or read fax status. Repeated failed sign-ins are slowed down. Both return HTTP 429 with a `Retry-After` header.

## What a refused request looks like

- **401**: the credential is missing, wrong, expired, revoked, or belongs to a disabled account.
- **403**: the credential is valid but lacks the permission, or the request came from an origin Faxbot does not trust.
- **404**: the fax or other item does not exist, or you are not allowed to know it exists.
