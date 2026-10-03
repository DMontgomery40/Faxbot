# Delivery routes

Faxbot records how every fax was sent and what it cost, and uses that to send each fax the cheapest way that works for its number.

Open **Tools → Delivery routes** in the Admin Console.

## What you see

- **Spending** shows the last 30 days per provider: faxes sent, faxes delivered, billed minutes, billed pages and the estimated cost.
- **Fax numbers** lists every number Faxbot has sent to, which routes reached it, how often each route delivered, and what the number cost.
- **Rate cards** hold the prices Faxbot uses for its estimates.
- **Direct partners** are organizations that receive your documents straight into their Faxbot, with no fax call. See [Direct delivery](direct-delivery.md).

## How Faxbot picks a route

For each fax, Faxbot puts the available routes in this order:

1. A verified direct partner for the number, if there is one.
2. The cheapest route that has worked reliably for that number.
3. The other routes, cheapest first.
4. Routes that have recently failed for that number. These come last.

A route counts as unreliable for a number after at least three finished faxes with less than 80% delivered in the last 30 days. Set `FAX_ROUTE_MIN_SUCCESS_PERCENT` to change that threshold.

You can override the order for one number. Open the number's **Details** and choose a **Preferred route**. Faxbot then tries that route first.

The route chosen for each fax, and the reason, is recorded before the fax is sent.

## Adding routes

Your outbound provider is always a route. To let Faxbot use more providers, list them in `FAX_OUTBOUND_ROUTES`:

```env
FAX_BACKEND=sip
FAX_OUTBOUND_ROUTES=signalwire,phaxio
```

Each listed provider needs its own settings filled in, as if it were the outbound provider. Faxbot uses the settings that were active when each fax was accepted.

A provider that is not ready is skipped. A cloud provider needs its credentials. An Asterisk route needs a connected Asterisk, and Faxbot connects to Asterisk only when Asterisk is your outbound or inbound provider. A common setup receives on your own Asterisk, sends through a cloud provider, and lists `sip` as an extra route. When a SIP route needs a fax image that the fax does not have yet, Faxbot makes one from the original PDF before sending.

## When a route fails

When a provider reports that a fax failed, Faxbot sends it again on the next route it has not tried yet. It does this at most twice per fax. Each retry is a new attempt on the same fax, and the fax history shows why it was retried. While the retry waits, the fax status shows `queued` again.

Faxbot never resends a fax whose outcome is unknown, for example when a provider stopped answering mid-request. That fax waits for confirmation from the provider, or from you.

## Costs

Faxbot keeps three amounts for each attempt:

- **Estimated** comes from your rate card and the call or pages Faxbot observed. It is rounded per call under the card's billing rule. With whole-minute billing, a 59-second call and a 61-second call bill as 1 and 2 minutes, which is 3 minutes in total.
- **Charged by the provider** is the amount the provider reports, when it reports one. Faxbot currently reads SignalWire's reported price. That price can arrive after the fax is delivered, so Faxbot keeps asking for up to 7 days.
- **Settled** is the provider's amount once it has stopped changing. A price seen a day after the call ended is treated as settled. Corrections from the provider replace the earlier amount.

A price the provider has not reported stays empty; Faxbot never fills it in with its own estimate. For a fax with an unknown outcome, the estimate assumes the fax was sent.

If a call record with measured connected time is available for an attempt, Faxbot uses it instead of its own timing, which includes queueing and ringing.

## Rate cards

Add one rate card per provider. Enter the provider's advertised price per minute, per page and per call, how they round call time, and where and when the price was advertised. Faxbot keeps old versions, so earlier estimates still show the price that applied.

These are examples of advertised prices (advertised on 2026-10-03). Check your own account, because prices vary by destination and plan:

| Provider | Product | Advertised price | Source |
| --- | --- | --- | --- |
| Telnyx | Elastic SIP trunk, US outbound | from $0.005 per minute | [telnyx.com/pricing/elastic-sip](https://telnyx.com/pricing/elastic-sip) |
| Telnyx | Elastic SIP trunk, US inbound | from $0.0032 per minute | [telnyx.com/pricing/elastic-sip](https://telnyx.com/pricing/elastic-sip) |
| Telnyx | Fax API, US | $0.007 per page, plus SIP usage | [telnyx.com/pricing/fax](https://telnyx.com/pricing/fax) |
| SignalWire | Fax, lower 48 states and Canada | $0.0095 per minute | [signalwire.com/pricing/fax](https://signalwire.com/pricing/fax) |

Running your own fax engine on a SIP trunk removes the per-page fee. For example, a 20-page fax takes about 10.5 minutes, billed as 11 whole minutes: about $0.055 over a trunk at $0.005 per minute, against about $0.195 through a per-page API at $0.007 per page plus the same minutes.

If the installation ships `config/rate_cards.json`, Faxbot loads it once as starting rate cards when you have none. Your edits always take precedence.

## Case packets

When you fax the same case to a recipient again and again, Faxbot can leave out the documents the recipient already has. Turn on **Accepts a one-page index instead of documents it already received for a case** in the number's **Details** only after the recipient agrees.

Send a case packet with `POST /cases/{case}/faxes`, using form fields `to`, one `documents` file per PDF, and an optional `titles` value for each. This needs `fax:send`. Faxbot identifies each document by the fingerprint of its exact bytes, so a corrected document counts as new. A document counts as accepted once a fax that carried it to that number for that case was delivered.

- When the recipient accepts references, the packet holds a one-page index listing the documents already accepted, followed by only the new or changed documents.
- Otherwise the packet holds every document.
- When nothing is new, Faxbot refuses the packet instead of faxing an index alone.

For example, a 40-page record and a cover letter go out as 41 pages. Each later update of four pages then goes out as 5 pages instead of 45. Add `preview=true` to see the packet without sending it. `GET /cases/{case}/documents?to=` lists what the recipient has accepted.

## API

These routes need `settings:read`, or `settings:write` for changes:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/routing/destinations` | Numbers with their routes, delivery rate and 30-day cost |
| GET | `/routing/destinations/{number}` | One number, with the route order for its next fax |
| PATCH | `/routing/destinations/{number}` | Name, notes, preferred route, case references |
| GET | `/routing/costs?since=` | Totals by provider |
| GET, PUT | `/routing/rate-cards` | Read or replace the rate cards |
