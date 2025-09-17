# Faxbot VPN Tunnel Implementation Plan (Revised)

## Overview
- Purpose: Provide secure remote connectivity for the Faxbot Admin Console and iOS app. A tunnel is required for the iOS app to reach the Faxbot server regardless of outbound fax backend (Phaxio, Sinch, SIP/Asterisk).
- Providers: Cloudflare Quick Tunnel (dev/non‑HIPAA), WireGuard (HIPAA‑capable), Tailscale (HIPAA‑capable), or None.
- UI‑first: All configuration and diagnostics are operable from the Admin Console with helper text and “Learn more” links built from `docsBase`. No CLI‑only features.

Key constraints from AGENTS.md
- Backend isolation: Tunnel guidance is backend‑agnostic and must not mix outbound provider setup details on the same panel. Contextual notes can mention Phaxio/Sinch/SIP only to clarify tunnel necessity for the iOS app.
- Security/PHI: No PHI or secrets in logs/UI. Mask secrets. Terminal is local‑only and must not be exposed via tunnels.
- Admin Actions: Any container checks or controlled start/stop operations run only via the allowlisted Admin Actions API and are gated by `ENABLE_LOCAL_ADMIN` and `ENABLE_ADMIN_EXEC`.

## 1) Security Posture and HIPAA Rules
- Cloudflare Quick Tunnel (trycloudflare.com)
  - Not HIPAA compliant. No BAA. Ephemeral URL. Do not use for PHI.
  - Gate with a clear warning and auto‑disable when HIPAA posture is enabled (e.g., `ENFORCE_PUBLIC_HTTPS=true` or an explicit HIPAA mode flag).
  - Allowed for non‑PHI development and quick trials only.
- Cloudflare Named Tunnel (Phase 2, optional)
  - If implemented later, require: Cloudflare account, owned domain, Access policies, and BAA. Only then may it be used with PHI.
- WireGuard (HIPAA‑capable)
  - Site‑to‑site or client tunnel under your control (e.g., Firewalla). Keep AMI/SIP/UDPTL private; only API port 8080 behind TLS is public.
- Tailscale (HIPAA‑capable)
  - Tailnet access with ACLs; treat as a private network path.
- Admin Console safeguards
  - Never expose `/admin/terminal` or Admin Actions via any public tunnel. Enforce local‑only gating: `ENABLE_LOCAL_ADMIN=true` and `ENABLE_ADMIN_EXEC=true` are required to reveal/run tests, and the server must verify the request originates from a local interface.
  - Redact/mask secrets in UI; do not display full tokens or keys; show last 4 only.

## 2) Configuration Model (env + persisted settings)
Environment variables (read on boot; UI persists durable settings in DB/config):
```env
# Core
TUNNEL_ENABLED=false
TUNNEL_PROVIDER=none  # none|cloudflare|wireguard|tailscale

# HIPAA posture (already exists in the app’s security model)
ENFORCE_PUBLIC_HTTPS=true

# WireGuard (client)
WIREGUARD_ENDPOINT=            # e.g., router.example.com:51820
WIREGUARD_SERVER_PUBLIC_KEY=
WIREGUARD_CLIENT_IP=           # e.g., 10.0.0.100/24
WIREGUARD_DNS=

# Tailscale
TAILSCALE_AUTHKEY=
TAILSCALE_HOSTNAME=faxbot-server
```
Notes
- Default to `TUNNEL_PROVIDER=none` to avoid unexpected exposure.
- Do not store secrets in plain env via UI. Persist in the server’s secure settings store with redaction in responses.

## 3) Admin API (FastAPI) — Admin‑only endpoints
Scopes and auth
- Admin‑only. Reuse the existing admin auth path (API key with `keys:manage` or equivalent admin scope).
- Standard error mapping: 400/401/404/413/415 as defined in AGENTS.md.

Endpoints (added to OpenAPI and client types)
- `GET /admin/tunnel/status` → TunnelStatus
- `POST /admin/tunnel/config` → apply provider config (validates + persists)
- `POST /admin/tunnel/test` → run safe connectivity probes and return results
- `POST /admin/tunnel/pair` → mint short‑lived pairing code for iOS QR
- `GET /admin/tunnel/qrcode?code=...` → return base64 PNG for QR (no secrets)

Models (conceptual)
```python
class TunnelStatus(BaseModel):
    enabled: bool
    provider: Literal["none","cloudflare","wireguard","tailscale"]
    status: Literal["disabled","connecting","connected","error"]
    public_url: Optional[str] = None   # Cloudflare dev only; hidden in HIPAA mode
    local_ip: Optional[str] = None
    last_checked: datetime
    error_message: Optional[str] = None

class TunnelConfig(BaseModel):
    enabled: bool
    provider: Literal["none","cloudflare","wireguard","tailscale"]
    # Provider‑specific nested config; secrets are write‑only
    cloudflare: Optional[CloudflareQuickConfig]
    wireguard: Optional[WireGuardConfig]
    tailscale: Optional[TailscaleConfig]
```

Implementation notes
- Do not orchestrate Docker directly from these endpoints. For any start/stop operations, call the Admin Actions framework (see section 5) which is local‑only and allowlisted.
- Cloudflare Quick Tunnel URL discovery (dev‑only): read the last N lines of the `cloudflared` container logs via an allowlisted Admin Action and parse `https://*.trycloudflare.com`. This behavior is disabled in HIPAA posture.
- Persist UI settings in the server settings store; never echo secrets back. Expose redacted values only.
- Pairing: `POST /admin/tunnel/pair` returns `{ code, expires_at, qr_png_base64 }`. The QR encodes a short code only. The iOS app exchanges the code for connection details using a one‑time token flow; no API keys in the QR.

## 4) Admin Actions (local‑only, allowlisted)
Extend `GET /admin/actions` and `POST /admin/actions/run` with the following safe actions (executed only when `ENABLE_LOCAL_ADMIN && ENABLE_ADMIN_EXEC` are true and request is local):
- `tunnel_status_cloudflared_logs_tail` → tail 50 lines of `cloudflared` logs
- `tunnel_start_cloudflared` / `tunnel_stop_cloudflared` → `docker compose --profile cloudflare up -d` / `down`
- `tunnel_start_wireguard` / `tunnel_stop_wireguard`
- `tunnel_start_tailscale` / `tunnel_stop_tailscale`
- `tunnel_probe_http` → perform an internal HTTP(S) HEAD to the expected URL

All actions are strictly parameterless or use validated, fixed arguments. No arbitrary command execution.

## 5) Docker Compose Profiles (documentation + optional Admin Action control)
Add profiles to `docker-compose.yml` (documentation snippet):
```yaml
services:
  # Cloudflare Quick Tunnel (dev/non‑HIPAA)
  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: faxbot-cloudflared
    restart: unless-stopped
    command: tunnel --no-autoupdate --url http://api:8080
    profiles: [cloudflare]
    depends_on: [api]
    networks: [faxbot-net]

  # WireGuard client (HIPAA‑capable; bring your own server)
  wireguard:
    image: linuxserver/wireguard:latest
    container_name: faxbot-wireguard
    cap_add: [NET_ADMIN, SYS_MODULE]
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - ./wireguard-config:/config
      - /lib/modules:/lib/modules
    profiles: [wireguard]
    restart: unless-stopped
    networks: [faxbot-net]

  # Tailscale client (HIPAA‑capable)
  tailscale:
    image: tailscale/tailscale:latest
    container_name: faxbot-tailscale
    hostname: ${TAILSCALE_HOSTNAME:-faxbot-server}
    environment:
      - TS_AUTHKEY=${TAILSCALE_AUTHKEY}
      - TS_HOSTNAME=${TAILSCALE_HOSTNAME:-faxbot-server}
    volumes:
      - tailscale-data:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    cap_add: [NET_ADMIN, SYS_MODULE]
    profiles: [tailscale]
    restart: unless-stopped
    networks: [faxbot-net]

volumes:
  tailscale-data:
```

Notes
- Do not publish new ports publicly. Treat tunnels as private ingress paths.
- Keep AMI/SIP/UDPTL private. The tunnel is for Admin Console/iOS connectivity, not telephony media.

## 6) Admin Console UI (Vite/React/TS/MUI)
Goals (per AGENTS.md)
- Use `ResponsiveSettingSection`, `ResponsiveFormSection`, and form kit components from `api/admin_ui/src/components/common/ResponsiveFormFields.tsx` and `ResponsiveSettingItem.tsx`.
- Provide helper text for each control and at least one “Learn more” link built from `docsBase`.
- Mask secrets (`type="password"` with show/hide; or `SecretInput` if present). Never log secrets.
- Mobile‑first layout; use MUI transitions; show loaders and disabled states.

Component: `api/admin_ui/src/components/TunnelSettings.tsx` (outline)
```tsx
type Props = { client: AdminAPIClient; docsBase?: string; hipaaMode?: boolean };

export default function TunnelSettings({ client, docsBase, hipaaMode }: Props) {
  // Fetch status via client.getTunnelStatus(); render provider select via ResponsiveSelect
  // Provider options: Cloudflare (disabled with warning when hipaaMode), WireGuard, Tailscale, None
  // Display status card with SmoothLoader, actionable errors, and test/connectivity button
  // Pairing: call client.createTunnelPairing() to get QR (base64 PNG); render with <img src={`data:image/png;base64,${png}`} />
  // Add a Link: <Link href={`${docsBase}/networking/tunnels`}>Learn more</Link>
}
```

UI controls (examples)
- Provider select (ResponsiveSelect)
  - Helper: “Required for iOS connectivity. Choose a provider that matches your security needs.”
  - When HIPAA posture is on: show `Cloudflare (dev only)` disabled with tooltip: “Not HIPAA compliant. Use WireGuard or Tailscale.”
- WireGuard fields
  - `endpoint`, `server_public_key`, `client_ip`, `dns` (masked as needed)
  - Helper: “Connects to your existing WireGuard server (e.g., Firewalla).”
- Tailscale fields
  - `auth_key` (masked), `hostname`
  - Helper: “Joins your Tailnet. Manage access via Tailscale ACLs.”
- Status + Actions
  - “Test Connectivity” button → `client.testTunnel()`; show success path hint and remediation on failure.
  - “Generate iOS Pairing QR” → `client.createTunnelPairing()`; never include secrets in QR.

Error handling
- 400 invalid input → inline field errors and helper text
- 401 auth → “Invalid API key or insufficient permissions”
- 404 → “Resource not found or expired”
- 413/415 → keep consistent with global messages

Integration in Settings.tsx
- Add a `ResponsiveSettingSection` titled “VPN Tunnel” with an icon and short description.
- Pass `docsBase={adminConfig?.branding?.docs_base}` and `hipaaMode={security?.hipaaEnabled}` if available.

## 7) iOS App Pairing Flow (no secrets in QR)
1. Admin presses “Generate iOS Pairing QR”.
2. Server mints `{ code, expires_at }` (e.g., 5 minutes) and returns `qr_png_base64` encoding only the short code.
3. The iOS app scans QR and calls `POST /mobile/pair` with the code to obtain a one‑time token and the appropriate base URL (local and/or tunnel), then discards the code.
4. App stores the base URL and uses normal API auth (API key created via Admin Console or OAuth, depending on transport).

Security notes
- No API keys or secrets in the QR or in logs.
- Rate‑limit pairing and invalidate codes on first use or expiry.

## 8) Diagnostics and Scripts & Tests
- Add backend‑aware checks under Tools → Scripts & Tests (local‑only):
  - “Tunnel URL reachable” (HTTP HEAD)
  - “LAN reachability” (local IP check)
  - “Cloudflare logs tail (last 50)” (dev only)
- Each check shows actionable remediation with a “Learn more” link and doesn’t leak secrets or PHI.

## 9) Acceptance Criteria
- UI parity
  - Uses responsive kits; helper text on all controls; at least one Learn more link via `docsBase`.
  - Mobile‑first verified at xs/sm/md; transitions 200–400ms; loaders/disabled states present.
- Security
  - Cloudflare Quick Tunnel disabled automatically in HIPAA posture with a clear warning.
  - No secrets in QR, logs, or UI; secrets masked.
  - Terminal and Admin Actions remain local‑only and not reachable via tunnels.
- API/OpenAPI
  - Endpoints added to OpenAPI; `client.ts`/`types.ts` updated with correct types.
  - Error mapping matches global rules; admin‑only auth enforced.
- Backend isolation
  - Copy does not mix outbound provider instructions; only states that tunnels are required for iOS connectivity regardless of backend.

## 10) Copy Examples (short, plain)
- Cloudflare (dev only): “Quick setup for non‑PHI testing. Not HIPAA compliant. Use WireGuard or Tailscale for PHI.”
- WireGuard: “Connect to your existing WireGuard server (e.g., Firewalla). Recommended for HIPAA.”
- Tailscale: “Join your Tailnet to access Faxbot over a private network. Recommended for HIPAA.”
- Pairing: “Generate a short‑lived QR to connect the iOS app. No secrets included.”

## 11) Out‑of‑Scope/Phase 2
- Cloudflare Named Tunnel with BAA and Access policies (HIPAA‑capable variant).
- Automated certificate and domain wiring for named tunnels.

This plan satisfies AGENTS.md: GUI‑first, backend isolation, secure defaults for HIPAA users, helpful inline guidance with docs links, consistent error handling, and local‑only admin execution for any container checks.
