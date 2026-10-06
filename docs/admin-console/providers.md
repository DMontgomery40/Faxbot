# Providers

The panel lists only what this installation uses: **In use**, one page per provider in use, named the way you know it ("HumbleFax", "Telnyx", "Avaya IP Office"), and **Add or change a provider**, which opens the [Setup wizard](setup-wizard.md). Pages for providers not in use keep their addresses, such as `#/providers/phaxio`.

## In use

**Providers → In use** (`#/providers/sending`) shows what sends, what receives and further sending routes, each opening its page, then:

- **Sending is on**: turning it off asks first, in one sentence; faxes submitted while sending is off stay on hold.
- **Receiving is on**: says which provider receives ("Faxes arrive through Telnyx."). It cannot be turned on with a provider that only sends.
- How long received faxes are kept, and how long their download links work.
- Delivery routes: extra providers a fax may use and how reliable a route must be.
- The addresses to give a cloud provider for receiving.

## Each provider

A provider's page says in one sentence whether Faxbot uses it now, then shows its account: keys and secrets (a value set in `.env` shows **Set in .env**), addresses for status updates, and signing checks.

- **The carrier trunk** is titled by its carrier or phone system. It opens with that carrier's guidance and settings, the fax numbers, **Check the internet address every … minutes**, and for Telnyx the optional **Telnyx API key**, which shows call charges and checks fax over IP (T.38) on your numbers. On the trunk page, **Fax over IP (T.38) at Telnyx** shows each number's setting and lets you turn it on for one number. The fax engine connection sits in a collapsed **Fax engine connection (advanced)** box with the read-only ports and phone system address. See [SIP trunk](../setup/sip-trunk.md).
- **FreeSWITCH (advanced)** says what it still needs, such as a caller ID number.
- **HumbleFax** lists the numbers on its account under **Numbers → Your numbers**.
