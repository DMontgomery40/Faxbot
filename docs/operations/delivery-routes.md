# Delivery routes

Faxbot records how every fax was sent and what it cost, and uses that to send each fax the cheapest way that works for its number.

Open **Tools → Delivery routes** in the Admin Console.

## What you see

- **Spending** shows the last 30 days per route: faxes sent and delivered, billed minutes, what the carrier charged, an estimate for faxes it has not billed yet, and how many are still waiting for the carrier's bill. Calls received on your SIP trunk get their own card. See [Costs](#costs).
- **Fax numbers** lists every number Faxbot has sent to, which routes reached it, how often each route delivered, and what the number cost. A number's **Details** also has its [sending together](#sending-together) setting.
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

## Sending together

Faxbot can hold short faxes to the same number for a few minutes and send them together in one call. This helps on a SIP trunk billed by the minute with a minimum. On Telnyx, for example, every call is billed at least one minute, so three one-page faxes sent separately cost three minutes but together cost about two.

It is off for every number. Turn it on for one number in its **Details**, under **Sending together**. You must first tick **This recipient has agreed to receive several documents in one call.** Faxbot keeps a record of who turned it on or changed it, and when.

- **Longest wait** (default 10 minutes) is how long a fax may wait for others.
- **Most pages in one call** (default 30) counts the separator pages.
- **Faxes from different senders may share a call** is off by default. While it is off, only faxes from the same person or API key go together.

Faxbot holds a fax only when all of these are true:

- the fax goes over Faxbot's own SIP trunk;
- the trunk's rate card charges per call, or bills with a minimum of at least 30 seconds;
- the fax fits in one call together with its separator page.

On per-page and flat-plan routes, faxes go straight away, and the setting says why in one sentence. For example: "On HumbleFax's flat plan, sending together saves nothing, so faxes go straight away."

A waiting fax goes when any of these happens:

- its longest wait ends;
- the next fax for that number would not fit in the call;
- someone chooses **Send now** in Jobs, the **Send now** box on Send, or `faxbot send --now`. This takes the faxes already waiting for that number with it.

One call carries the documents in the order they were accepted. Each document follows a separator page, for example "Document 2 of 3 · Faxbot 7f3a9c21 · 4 pages · from Front Desk". The reference is the sender's case reference when the fax belongs to one. Otherwise it is "Faxbot" and the first 8 characters of the fax's ID. Job Details shows the same reference.

Each fax keeps its own record and its own result. The receiving machine confirms pages in order:

- A fax whose separator and pages were all confirmed is delivered.
- A fax the call never reached failed, as a single fax would. Faxbot may try it on the next route.
- If the call failed after part of a fax was confirmed, that fax failed with a sentence saying how many of its pages were confirmed. Faxbot never sends it again by itself; a person decides.
- If the call ended with no page count, every fax in it waits for a person, like any fax whose outcome is unknown.

Faxbot does not send a shared call again automatically. If the SIP trunk is unavailable when the faxes are due, each fax goes on its own instead. A fax whose document cannot be read goes on its own; the others still go together.

Job Details says "Sent in one call with 2 other faxes" and shows the fax's share of the call's charge. The charge is split by pages, each fax with its separator page. The number's **Details** shows how many calls were saved in the last 30 days and about how much money. That figure is an estimate: separate calls are priced from the rate card the way Faxbot estimates any fax, and compared with the call's reported charge, or with its estimate when no charge has been reported. In Spending, a shared call counts once.

From the command line: `faxbot routing batching show|set|off NUMBER` (`set` takes `--recipient-agreed`, `--wait`, `--max-pages` and `--mixed-senders`), `faxbot send --now` and `faxbot jobs send-now FAX_ID`.

Tested on 4 October 2026 over a local T.38 test line between two Faxbot fax engines (`make native-proof`). One call carried two faxes, 5 pages including separators, in 39 seconds, and both were delivered with identical pages. A call ended after 27 seconds: the sender counted 2 confirmed pages, the first fax was delivered and the second failed. In an earlier cut, the receiver held 1 page while the sender counted 0, so the receiver can hold one page more than the sender saw confirmed. Not yet confirmed on a live trunk.

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
- how many could not be matched to exactly one carrier record (their cost stays unknown);
- calls the carrier billed that Faxbot has no record of, for example a received fax whose hand-over failed before Faxbot recorded its call. Their charge is part of the charged total and is listed on its own line, such as "Telnyx billed 1 call Faxbot has no record of: $0.0032."

The Dashboard's **Spending, last 30 days** card reads the same figures: one line per sending route, one for calls received on the SIP trunk, and the total. A provider with no published price and no rate card reads "No published price; add your rate".

Faxbot finds calls it has no record of by reading the trunk's Telnyx records for the last two days once an hour. It only counts priced records on the trunk's own numbers (its fax numbers and caller ID) that are not within five minutes of any call Faxbot recorded with the same numbers. When exactly one fax received over the trunk, with no call record, was received within 45 seconds of the call's end and has the same numbers where it knows them, the charge is shown on that fax in the Inbox, and Spending says the call "reached Faxbot without a call record; its fax is in the Inbox". For a fax brought in later, Faxbot uses the time the trunk received it; a fax whose numbers were never learned is matched by that time alone, and only when no other such fax or record is that close. The charged line counts these calls too, for example "Telnyx charged $0.0096 for 3 calls, 1 without a Faxbot call record."

**Job Details** shows one fax's cost, for example "Telnyx charged $0.005 for this call." or "Cost not reported yet." The Inbox shows the same line for each received fax under **Received through**.

From the command line:

```bash
faxbot routing costs                 # spending per route and for received calls
faxbot routing reconcile             # ask Telnyx now, instead of waiting for the next check
faxbot routing fax-cost FAX_ID       # one sent fax; add --received for a received fax
```

If a call record with measured connected time is available for an attempt, Faxbot uses it instead of its own timing, which includes queueing and ringing.

### Flat monthly plans

Some providers charge a monthly fee and nothing per fax. Give that provider a rate card with a **Monthly plan fee** and leave the per-minute, per-page and per-call prices at 0. Faxbot then shows the route as "Included in your HumbleFax plan ($10 a month)" in Spending, on the Dashboard and in route recommendations, and ranks it as costing nothing extra per fax. The fee is never added to one fax. In spending totals the fee counts once per 30 days, pro-rated by day for other periods.

### Published prices Faxbot ships

Every time Faxbot starts, each provider you send or receive with that has never had a rate card gets the card below, marked with its advertised date and source. You can edit or remove it like any other card. Faxbot records the addition in the audit log, and a card you removed or replaced is never added back. Prices were read on 2026-10-03 from each provider's own pages:

| Provider | Price | Source |
| --- | --- | --- |
| Phaxio, sending and receiving (US and Canada) | 7 cents per page; 5 cents from 50,000 pages a month, 4 cents from 100,000; numbers $2 a month | [phaxio.com/pricing](https://www.phaxio.com/pricing/), [phaxio.com](https://www.phaxio.com/) |
| Sinch Fax API, sending and receiving | $0.045 per page | [sinch.com/voice/fax-api](https://sinch.com/voice/fax-api/) |
| SignalWire Fax, sending and receiving | $0.0095 per minute (48 contiguous states and Canada); no billing increment published, so whole minutes are assumed | [signalwire.com/pricing/fax](https://signalwire.com/pricing/fax) |
| HumbleFax | $10 a month to send and receive unlimited faxes, no overage charges | [humblefax.com/faq](https://humblefax.com/faq) |
| Your SIP trunk | the trunk carrier's per-minute prices in [What a call costs](../setup/sip-trunk.md#what-a-call-costs) | carrier pages |
| Documo | no published fax API price; add your contracted rate | [documo.com/pricing](https://www.documo.com/pricing/) |
| eFax | the API is priced by quote, so no card; **Rate cards** names eFax's cheapest published plan for your installation country and **Use a published plan as my estimate** adds it as your own card when you choose (`faxbot routing plans efax`) | [eFax](../setup/efax.md#prices) |

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
| GET, PUT, DELETE | `/batching/numbers/{number}` | Sending together for one number: its setting and history, the reason it saves money or not, and calls saved. PUT takes `enabled`, `recipient_agreed`, `max_wait_minutes`, `max_pages`, `mixed_senders` and `version` |

Anyone who may send faxes can ask whether a number sends faxes together: `GET /batching/check?to=`. Anyone who may read a fax can see whether it is waiting or went with others, and its share of the charge: `GET /batching/faxes/{id}`. They can also send a waiting fax now: `POST /batching/faxes/{id}/send-now`. `POST /fax` takes an optional `send_now=true`.

Anyone who may read a fax can read its cost: `GET /routing/faxes/{id}/cost` for a sent fax, `GET /routing/inbound/{id}/cost` for a received fax, and `GET /routing/inbound-costs?ids=` for up to 100 received faxes at once. Faxes the person cannot read are left out.
