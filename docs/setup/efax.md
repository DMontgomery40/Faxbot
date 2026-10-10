# eFax

eFax (Consensus Cloud Solutions) sells cloud fax in the US, the UK and Australia. Its business plans include the **eFax Enterprise API**, sold as part of **eFax Corporate**, and Faxbot uses that API to send and receive. The API is the same in all three countries. Faxbot sends each fax to one number and follows its status with eFax. It receives by asking eFax for new faxes, so nothing has to reach Faxbot from the internet.

## Get API access

eFax gives API access through its sales team; there is no self-service sign-up.

1. Ask eFax for eFax Corporate with API access: [eFax US](https://www.efax.com/products/fax-api), [eFax UK and Europe](https://enterprise.efax.eu/) or [eFax Australia](https://www.efax.com.au/).
2. eFax sends a welcome email with three values:
    - **App ID**: the API instance. eFax can give you several (for example a test one and a live one); each is a separate environment with its own faxes.
    - **API key**: the secret that goes with the App ID.
    - **User ID**: the eFax user, and with it the fax number, that sends and receives.
3. Keep the API key in a password manager. eFax asks that it never travels in the same message as the App ID.
4. Use a separate App ID for each Faxbot installation (eFax issues test and live ones). Signing in to eFax ends the previous sign-in for that App ID, so two installations sharing one keep signing each other out.
5. eFax provides a demo fax number for testing; you can send to it and receive on it at the same time.

## Set it up in Faxbot

Put the three values in `.env` and start Faxbot again with `docker compose up -d`:

```
EFAX_APP_ID=...
EFAX_API_KEY=...
EFAX_USER_ID=...
```

Faxbot reads them at every start. The Setup Wizard and Settings show them as **Set in .env**; change them there. You can also type them into the Setup Wizard or Settings instead; Faxbot keeps them encrypted in its database.

Then, in the **Setup Wizard** (or **Delivery setup → eFax** when it is already in use):

1. Choose **eFax** for **Sending**, **Receiving** or both.
2. In the eFax section, optionally enter:
    - **Caller ID**: the eFax number recipients see. Leave it empty for your account's number. eFax accepts only numbers on your account.
    - **Station name**: up to 20 characters the receiving fax machine shows as the sender.
3. When eFax receives, choose how often Faxbot checks eFax for received faxes (every minute by default) and whether Faxbot deletes each fax from eFax once it has stored it.

The settings in `.env` that are not keys are read only on the first start; change them in Settings after that:

| Setting | What it is | Default |
| --- | --- | --- |
| `EFAX_CALLER_ID` | The eFax number recipients see | Your account's number |
| `EFAX_CSID` | Station name, up to 20 characters | eFax's default |
| `EFAX_POLL_SECONDS` | How often Faxbot checks eFax for received faxes, 30 to 3600 seconds | 60 |
| `EFAX_DELETE_AFTER_DOWNLOAD` | Delete each received fax from eFax once Faxbot stored it | `false` |

`EFAX_WEBHOOK_SECRET`, for [notifications](#notifications-optional), is a key like the other three: Faxbot reads it at every start.

### Check the keys

```
EFAX_APP_ID=... EFAX_API_KEY=... EFAX_USER_ID=... faxbot admin settings validate efax
```

This says whether eFax's API answers and whether eFax accepts the keys. It sends no fax. Signing in to eFax ends the sign-in Faxbot was using; Faxbot signs in again by itself on its next request.

Then send a test page to eFax's demo number from **Faxes → Send a fax** or with `faxbot send`, and follow it in **Faxes → Sent**.

## Sending

Faxbot sends one fax to one number. Numbers in the US and Canada go to eFax as 1 and ten digits; other numbers keep their + and country code. Faxbot sends the prepared PDF at fine resolution with no eFax cover page, and records its own attempt as eFax's client reference so you can find the fax in eFax.

Faxbot signs in once and shares that sign-in for 24 hours, as eFax asks. If eFax refuses the keys when Faxbot signs in, the fax fails before anything is sent. If eFax answers a request with "sign in again", Faxbot signs in again and repeats that one request, because eFax refused it before reading it.

Faxbot asks eFax for each sent fax's progress until it finishes:

| eFax says | Faxbot shows |
| --- | --- |
| New, in progress, waiting to retry | Sending |
| Complete | Delivered |
| Error | Failed |
| Canceled | Cancelled |

Anything else, or no answer, leaves the fax as it was. A send whose reply is lost (a timeout, an unreadable answer) is never sent again by itself: the fax waits for a person in **Faxes → Sent**, where you can enter the eFax fax ID once you have found it in eFax. eFax can stop a fax only before it is delivered, and an accepted request to stop is not proof that it stopped; Faxbot does not offer cancelling eFax faxes yet.

## Receiving

Faxbot asks eFax for the faxes it lists as not downloaded yet, every minute by default, 100 at a time and up to 1,000 per check. For each new fax:

1. Faxbot records the fax first (it shows as **Waiting for the document from eFax.**), because reading a fax from eFax marks it as downloaded there.
2. It then downloads the fax by its eFax ID as a PDF, checks it and stores it. A fax that cannot be fetched is tried again on Faxbot's usual schedule and can be fetched again from **Faxes → Received**. If Faxbot stops in between, it carries on with the same fax after a restart.
3. Once the fax is stored, Faxbot tells eFax it was downloaded and, if you turned deletion on, deletes it from eFax.

If eFax refuses a deletion, the fax stays at eFax and Faxbot tries again on each check, waiting longer each time (up to 30 minutes) for seven days. The eFax section in Settings and `faxbot faxes received list` say "1 received fax is still stored at eFax; Faxbot will try again to delete it." After seven days they say to delete it in your eFax account.

If eFax asks Faxbot to slow down, Faxbot waits as long as eFax says. After a failed check it waits longer each time, up to 30 minutes. Settings shows when Faxbot last checked eFax.

Things to know:

- Faxbot only collects faxes eFax lists as not downloaded. If another program, or eFax's own web portal, downloads a fax first, Faxbot does not see it.
- Faxbot only asks eFax's API address, `https://api.securedocex.com`, with the account in settings, and never follows an address from a reply. If you change the App ID or User ID, faxes that arrived on the earlier account are not fetched with the new one; Received says so.

### Notifications (optional)

Checking every minute needs no public address. If Faxbot has one, eFax can also tell Faxbot the moment a fax arrives:

1. Choose a long random notification secret and put it in `.env` as `EFAX_WEBHOOK_SECRET` (or in the eFax section of Settings).
2. Ask eFax (your account team) to send inbound notifications to `https://<your Faxbot address>/efax-inbound` with HMAC authentication using that secret. eFax requires HTTPS on port 443 or 8443.

Faxbot accepts a notification only when its `X-HMAC-Signature` header is the hex HMAC-SHA256 of the message with the secret, and then only starts its next check of eFax at once. It never uses the notification's contents: the fax and its details always come from eFax's API, as above. If eFax has asked Faxbot to wait, a notification does not start a check sooner, and checks are never closer than five seconds apart. eFax's two code samples disagree on how to compare the signature (one compares decoded bytes, one compares hex text); both describe a hex digest of the message, which is what Faxbot checks, ignoring letter case.

## Prices

eFax prices the API by quote in every country, so Faxbot gives eFax no starting rate card and shows **No published price; add your rate**. **Savings & optimization → Spending** and **Savings & optimization → Prices & plans** name eFax's cheapest published plan for your installation country on the eFax line, and **Use a published plan as my estimate** opens a new rate card filled in from it, saved only when you click Save. `faxbot savings plans efax` lists the same plans.

| Plan | Country | Price a month | Includes | Extra page | Read on |
| --- | --- | --- | --- | --- | --- |
| [eFax Personal](https://www.efax.com/pricing) | US | USD 18.99 (15.83 billed annually) | 200 pages to the US and Canada | USD 0.10 | 4 October 2026 |
| [eFax Business](https://www.efax.com/pricing) | US | USD 39.99 (33.33 billed annually) | 500 pages to 51 countries, 5 users | USD 0.07 | 4 October 2026 |
| [eFax Corporate](https://www.efax.com/pricing) (API) | US | By quote | | | 4 October 2026 |
| [eFax Plus](https://www.efax.com.au/pricing/efax/) | Australia | AUD 16.95 before GST (14.13 billed annually), AUD 10 set-up | 150 pages sent and 150 received | AUD 0.10 | 4 October 2026 |
| [eFax Pro](https://www.efax.com.au/pricing/efax/) | Australia | AUD 18.95 before GST | 200 pages sent and 200 received | AUD 0.10 | 4 October 2026 |
| [eFax Pro2](https://www.efax.com.au/pricing/efax/) | Australia | AUD 23.95 before GST | 300 pages sent and 300 received | AUD 0.10 | 4 October 2026 |
| [eFax Corporate](https://www.efax.com.au/pricing/efax/) (API) | Australia | By quote | | | 4 October 2026 |
| [eFax UK](https://ww2.efax.com/uk/) | UK | Not readable | | | 4 October 2026 |

The US page offers the API with eFax Corporate only; the Australian page lists the API as available on its other plans too, which eFax Australia should confirm. The UK site's plan links led to the US price page and a US dollar checkout when read from the US, so no UK price could be read. A rate card holds a monthly fee but not a page allowance, so an estimate made from a plan counts the monthly fee and not extra pages.

## What has been checked

Faxbot's eFax support was built from eFax's published API specification and getting-started pages (read 3 October 2026) and tested against a fake eFax that answers with the published request and reply shapes. It has not yet been run against a real eFax account. Not published, and so not known: request limits beyond eFax's stated 100 MB per fax, how often eFax lets you check for received faxes, and whether UK or Australian accounts use a different API address.

## References

- [eFax Fax Services API specification](https://consensus.stoplight.io/docs/fax-services)
- [eFax Enterprise API getting-started pages](https://docs.efaxcorporate.com/API/): credentials, receiving faxes, sent fax status, cancelling a fax and system characteristics
- [eFax's Postman collection](https://www.postman.com/collections/268299a822d69a8a05c5)
