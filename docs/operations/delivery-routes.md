# Delivery routes

Faxbot records how every fax was sent and what it cost, and uses that to send each fax the cheapest way that works for its number.

Open **Tools → Delivery routes** in the Admin Console.

## What you see

- **Spending** shows the last 30 days per route: faxes sent and delivered, billed minutes, what the carrier charged, an estimate for faxes it has not billed yet, and how many are still waiting for the carrier's bill. Calls received on your SIP trunk get their own card. See [Costs](#costs).
- **Fax numbers** lists every number Faxbot has sent to, which routes reached it, how often each route delivered, and what the number cost.
- **Rate cards** hold the prices Faxbot uses for its estimates.
- **Direct partners** are organizations that receive your documents straight into their Faxbot, with no fax call. See [Direct delivery](direct-delivery.md).

## How Faxbot picks a route

For each fax, Faxbot puts the available routes in this order:

1. A verified direct partner for the number, if there is one.
2. The cheapest route that has worked reliably for that number.
3. The other routes, cheapest first. A route without a known price comes after the routes with one.
4. Routes that have recently failed for that number. These come last.

Each route in a number's **Details** says what decided its place:

| Reason | Meaning |
| --- | --- |
| The cheapest route that works reliably for this number. | Every reliable route has a known price, and this one costs least. |
| The cheapest route with a known price that works reliably for this number. | Another reliable route has no rate card, so Faxbot cannot say it is the cheapest overall. |
| Included in your HumbleFax plan. | The route's rate card is a flat monthly plan, so a fax adds nothing. |
| More reliable for this number; its cost is unknown. | Cheaper routes often failed for this number, and this route has no rate card. |
| Your outbound fax provider; its cost is unknown. | No route has a known price, so your configured order decides. |
| Your outbound fax provider. | There is only one route to choose from. |

A route whose cost is unknown is never called the cheapest.

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

A provider that is not ready is skipped. A cloud provider needs its credentials. An Asterisk route needs a connected Asterisk: Faxbot connects to it when Asterisk is your outbound or inbound provider, or when `sip` is listed as an extra route. A common setup receives on your own Asterisk, sends through a cloud provider, and lists `sip` as an extra route. When a SIP route needs a fax image that the fax does not have yet, Faxbot makes one from the original PDF before sending.

## When a route fails

When a provider reports that a fax failed, Faxbot sends it again on the next route it has not tried yet. It does this at most twice per fax. Each retry is a new attempt on the same fax, and the fax history shows why it was retried. While another route remains, the fax status goes from `in_progress` back to `queued` and never shows `failed`.

Faxbot never resends a fax whose outcome is unknown, for example when a provider stopped answering mid-request. That fax waits for confirmation from the provider, or from you.

## Costs

Faxbot keeps three amounts for each attempt:

- **Estimated** comes from your rate card and the call or pages Faxbot observed. It is rounded per call under the card's billing rule. With whole-minute billing, a 59-second call and a 61-second call bill as 1 and 2 minutes, which is 3 minutes in total.
- **Charged** is the amount the provider or carrier reports. Faxbot reads SignalWire's reported fax price and, for faxes sent or received on a Telnyx SIP trunk, what Telnyx billed for each call (see [What a call costs](../setup/sip-trunk.md#what-a-call-costs)). A charge can arrive after the fax is delivered, so Faxbot keeps asking for up to 7 days.
- **Settled** is the charge once it has stopped changing. A charge seen a day after the call ended is treated as settled.

A charge the provider has not reported stays empty; Faxbot never fills it in with its own estimate. For a fax with an unknown outcome, the estimate assumes the fax was sent. A failed attempt that the carrier charged for counts in that fax's total. Billing never changes a fax's delivery status.

Spending adds up charges where they exist and estimates only for faxes without one, so the same fax is never counted twice. Each route card says:

- what the carrier charged, for how many faxes;
- the estimate for faxes not billed yet;
- how many are waiting for the carrier's bill;
- how many could not be matched to exactly one carrier record (their cost stays unknown).

**Job Details** shows one fax's cost, for example "Telnyx charged $0.005 for this call." or "Cost not reported yet." The Inbox shows the same line for each received fax under **Received through**.

From the command line:

```bash
faxbot routing costs                 # spending per route and for received calls
faxbot routing reconcile             # ask Telnyx now, instead of waiting for the next check
faxbot routing fax-cost FAX_ID       # one sent fax; add --received for a received fax
```

If a call record with measured connected time is available for an attempt, Faxbot uses it instead of its own timing, which includes queueing and ringing.

### Flat monthly plans

Some providers charge a monthly fee and nothing per fax. Give that provider a rate card with a **Monthly plan fee** and leave the per-minute, per-page and per-call prices at 0. Faxbot then shows its faxes as included in your plan, not as an unknown cost, and ranks the route as costing nothing extra per fax. The fee itself is shared by all faxes and is never added to one fax.

The shipped starting cards include the HumbleFax unlimited plan: the HumbleFax homepage, read on 2026-10-03, states "Unlimited Faxing $10 / month" with no monthly page limits. Starting cards only load into an installation that has no rate cards yet; add the plan in **Rate cards** otherwise.

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

## SSLFax

SSLFax is a HylaFAX+ feature. During an ordinary fax call, it moves the pages onto an encrypted internet connection, so the call ends sooner. It was tested on 2026-10-03 with HylaFAX+ 7.0.11 at both ends over a local test line. A 6-page fax took 12 seconds instead of 50, with identical pages, and fell back to an ordinary fax when the connection could not be made. It needs:

- a fax engine with SSLFax at both ends
- an audio (G.711) call rather than T.38
- an internet address the sender can reach, to receive this way

It saves money only on routes billed by the minute. Faxbot's Asterisk fax engine does not support SSLFax, so Faxbot does not currently offer it.

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
| GET | `/routing/costs?since=` | Spending by route and for received calls, with charged, estimated and waiting counts |
| POST | `/routing/reconcile` | Ask the SIP trunk carrier now what each open call cost (`settings:write`) |
| GET, PUT | `/routing/rate-cards` | Read or replace the rate cards |

Anyone who may read a fax can read its cost: `GET /routing/faxes/{id}/cost` for a sent fax, `GET /routing/inbound/{id}/cost` for a received fax, and `GET /routing/inbound-costs?ids=` for up to 100 received faxes at once. Faxes the person cannot read are left out.
