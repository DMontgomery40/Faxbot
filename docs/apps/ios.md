# iOS App

The iOS app lets you send faxes and check statuses from your phone. It connects to your own Faxbot server.

!!! success "Now on TestFlight"
    The iOS companion is available via TestFlight. Email david@faxbot.net for an invite.

[:material-email: Request Invite](mailto:david@faxbot.net?subject=Faxbot%20iOS%20TestFlight%20Invite){ .md-button .md-button--primary }

## Key points

- Faxbot does not provide a tunnel. Set up network access to your server using the [public access guide](../setup/public-access.md).
- Pairing uses a six-digit code from the admin console that lasts five minutes and works once. The QR code contains only that code, never a key.

## Setup overview

1. In **Administration → Keys & phones**, set **Address phones use on your network** to the server's address on your office network if phones will connect there. Leave it empty if phones connect only over the internet.
2. Configure network access to your server as described in the [public access guide](../setup/public-access.md).
3. Pair the app as described below.

## Pair the app

1. In the Admin Console, open **Administration → Keys & phones**.
2. Select **Pair a phone**. Faxbot shows a six-digit code, a QR code and a countdown.
3. In the iOS app, enter the code or scan the QR code before the countdown ends.

The code lasts five minutes and works once. If it expires or the app reports that it did not work, select **Pair a phone** again for a new code.

After pairing, Faxbot creates a key for that phone and returns it with the server's addresses. The key can send faxes, see sent faxes and their documents, and see received faxes and their documents. The key appears under the device's name in **Administration → Keys & phones**. Revoke it there if the phone is lost or replaced.

You need permission to pair phones and issue keys to create a pairing code. See [Access Control](../security/access-control.md#keys-for-the-iphone-app).

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
