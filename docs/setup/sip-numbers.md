# Fax numbers on SIP

An idle fax number can cost cents a month when it is hosted on a SIP carrier and received by Faxbot's own fax engine. You can choose where numbers live separately from how you send.

## Host numbers separately from sending

- **Receiving.** Keep or move your fax numbers to a SIP carrier that supports T.38, and receive them on Faxbot's Asterisk. Received faxes go to the [intake queue](../operations/intake.md).
- **Sending.** Choose the cheapest reliable way out, per destination. See [Delivery routes](../operations/delivery-routes.md).

Everyone who faxes you keeps dialing the same number. Moving a number to a new carrier uses the carrier's normal number port process.

Fax numbers entered in the console are saved in E.164 for the installation country (`FAX_DEFAULT_COUNTRY`), and received faxes are stored with E.164 numbers when they can be read.

## What numbers cost

These prices were advertised on 2026-10-03. Check the carrier's page and your account before you rely on them:

| Carrier | Number | Advertised price | Source |
| --- | --- | --- | --- |
| AnveoDirect | US geographic number, T.38 supported | $0.15 per month, $0.25 setup | [anveodirect.com/prices/did](https://anveodirect.com/prices/did) |
| AnveoDirect | US mobile number, T.38 supported | $1.00 per month, $5.00 setup | [anveodirect.com/prices/did](https://anveodirect.com/prices/did) |
| AnveoDirect | Per-minute number, incoming | $0.004 per minute, 10 channels per number | [anveodirect.com/prices/did](https://anveodirect.com/prices/did) |
| Telnyx | Number | $1.00 per month | [telnyx.com/pricing/fax](https://telnyx.com/pricing/fax) |
| Telnyx | Elastic SIP, US inbound | from $0.0032 per minute, T.38 included | [telnyx.com/pricing/elastic-sip](https://telnyx.com/pricing/elastic-sip) |

For 1,000 lightly used numbers, $0.15 a month instead of $1.00 a month is $850 a month saved before usage.

A carrier listing T.38 support does not guarantee that every call completes as T.38. Send test faxes in both directions before you move a busy number.

## Mobile numbers

There are two legitimate ways to use a mobile number for fax.

**A mobile number sold for SIP.** Some carriers sell numbers in the mobile range as SIP numbers with T.38, such as the AnveoDirect mobile numbers above. Faxbot receives them like any other SIP number. Such a number does not come with a phone or a retail calling plan.

**Forwarding from a phone.** A retail mobile line can forward calls to one of your Faxbot numbers with the carrier's call forwarding, either all calls during a "fax mode" or unanswered calls. The carrier bills forwarded calls under your plan, and text messages are not affected. Fax tones do not survive the phone's voice path well, so forward before the call reaches the handset. When many numbers forward to one Faxbot number, Faxbot needs reliable information from the carrier about which number was dialed. If it cannot tell, the fax goes to the unassigned inbox; Faxbot never guesses.

## What Faxbot does not do

- **Caller ID.** Faxbot shows only caller ID that your carrier has authorized for your account. It never presents a number you do not control.
- **TTY and RTT.** These carry text, not fax, so they cannot carry a fax call.

## Set up Asterisk

Follow [SIP/Asterisk](sip-asterisk.md) to connect your carrier trunk, then review the [SIP/Asterisk inbound webhook](webhooks.md#inbound-sipasterisk-selfhosted) and [intake queue](../operations/intake.md).
