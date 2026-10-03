# iOS App

The iOS app lets you send faxes and check statuses from your phone. It connects to your own Faxbot server.

!!! success "Now on TestFlight"
    The iOS companion is available via TestFlight. Email david@faxbot.net for an invite.

[:material-email: Request Invite](mailto:david@faxbot.net?subject=Faxbot%20iOS%20TestFlight%20Invite){ .md-button .md-button--primary }

## Key points

- A tunnel is required for the iOS app to reach your server (LAN‑only setups won’t work off‑network).
- HIPAA users should use a HIPAA‑capable tunnel (WireGuard or Tailscale). Avoid Cloudflare Quick Tunnels for PHI.
- Pairing uses a six-digit code from the admin console that lasts five minutes and works once. The QR code contains only that code, never a key.

## Setup overview

1. Open the Admin Console and locate the VPN/Tunnel settings.
2. Choose a provider:
   - WireGuard (HIPAA‑capable): connect to your existing WG server (e.g., Firewalla).
   - Tailscale (HIPAA‑capable): join your Tailnet with an appropriately scoped key.
   - Cloudflare Quick Tunnel (dev only): non‑PHI trials; not HIPAA‑compliant.
3. Pair the app as described below.

## Pair the app

1. In the admin console, open **Settings → Settings** and scroll to **VPN Tunnel**.
2. Select **Pair an iPhone**. Faxbot shows a six-digit code, a QR code and a countdown.
3. In the iOS app, enter the code or scan the QR code before the countdown ends.

The code lasts five minutes and works once. If it expires or the app reports that it did not work, select **Pair an iPhone** again for a new code.

When the app sends a valid code to `/mobile/pair`, Faxbot creates a key for that phone and returns it together with the server's addresses. The key can send faxes, see sent faxes and their documents, and see received faxes and their documents. It appears on **Settings → Keys** under the device's name. Revoke it there if the phone is lost or replaced.

Creating a pairing code needs the `tunnels:pair` permission and the right to issue keys (`keys:manage`), which Owners and Administrators have. See [Access Control](../security/access-control.md#keys-for-the-iphone-app).

## Learn more

- [Public Access & Tunnels](../setup/public-access.md)
- [Admin Console demo](../admin-console.md#demo-simulated)

## Screens (sneak peek)

<div class="grid cards" markdown>

- ![Send](../assets/images/ios_send_screen.png)
  
  Send screen

- ![Connect](../assets/images/ios_connect_to_server.png)
  
  Connect to Server

- ![Type Text](../assets/images/ios_txt_to_fax.png)
  
  TXT → Fax

</div>
