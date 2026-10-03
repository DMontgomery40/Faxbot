# Access and Sign-in API

The admin console's sign-in and access screens use two groups of routes. Their exact requests, responses and error codes are in the generated API reference:

- [Authentication routes (`/auth`)](../generated/api.html#tag/Authentication)
- [Access management routes (`/access`)](../generated/api.html#tag/Access-management)
- [OpenAPI JSON](../generated/openapi.json)

Your own server also publishes its reference at `/docs` and `/openapi.json`. For what the roles and permissions mean, see [Access Control](../security/access-control.md). For sessions, CSRF and transport rules, see [Authentication](../security/authentication.md).

## Sign-in routes

| Route | Use | Needs |
| --- | --- | --- |
| `POST /auth/login` | Sign in with a username and password | Trusted browser origin |
| `POST /auth/key-login` | Start a console session from an API key | Trusted browser origin |
| `GET /auth/me` | Who is signed in, their permissions, and the CSRF token for cookie sessions | Any credential |
| `GET /auth/context` | Which screens and send options to show | Any credential |
| `POST /auth/password` | Change your own password | Password session |
| `POST /auth/logout` | End the current session | Any session |
| `GET /auth/sessions` | List sessions | Your own; others need `sessions:read` |
| `POST /auth/sessions/{session_id}/revoke` | End a session | Your own; others need `sessions:revoke` |
| `POST /auth/owner/enroll` | Create the first owner | Installation key, while `/auth/me` reports `can_enroll_owner` |

## Access management routes

All of these need a credential. Reading needs the matching `:read` permission and changing needs the matching `:manage` permission.

| Routes | Covers | Permissions |
| --- | --- | --- |
| `/access/users`, `/access/integrations` | Users and integrations, password resets | `users:read`, `users:manage` |
| `/access/groups` | Groups and members | `groups:read`, `groups:manage` |
| `/access/roles`, `/access/permissions` | Roles and the permission list | `roles:read`, `roles:manage` (the permission list is open to anyone signed in) |
| `/access/assignments`, `/access/resources` | Who has which role where | `grants:read`, `grants:manage` |
| `/access/keys` | API keys: create, change, replace, revoke, approve | `keys:manage` |
| `/access/sessions` | Sessions | Your own; others need `sessions:read` or `sessions:revoke` |
| `/access/mailboxes`, `/access/inbound-rules` | Mailboxes and fax number routing | `mailboxes:read`, `mailboxes:manage` |
| `/access/audit` | Security audit, newest first | `audit:read` |

Changes send the `expected_policy_version` from the last read, and changes to one item also send that item's `version`. If someone else changed access in the meantime, the server answers 409; read again and retry.

## iPhone pairing

| Route | Use | Needs |
| --- | --- | --- |
| `POST /admin/tunnel/pair` | Create a six-digit pairing code | `tunnels:pair`, plus the right to issue the device's key |
| `POST /mobile/pair` | Exchange a pairing code for a device key | A current pairing code |

See [iOS App](../apps/ios.md#pair-the-app).
