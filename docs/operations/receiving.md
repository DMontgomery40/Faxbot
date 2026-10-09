# Receiving faxes

Faxbot only treats a fax as received once its real document has arrived and Faxbot has checked it. Until then, the Inbox shows the fax as waiting and nothing is emailed.

## What happens when a fax arrives

1. Phaxio, Sinch or Faxbot's own Asterisk engine tells Faxbot that a fax arrived.
2. Faxbot checks that the notification is genuine before it records anything. See [how notifications are checked](#how-notifications-are-checked).
3. The fax appears in the Inbox as **Waiting for the document**.
4. Faxbot gets the document. For Phaxio and Sinch it downloads it from the provider's API by the fax ID. For Asterisk it converts the image the call produced.
5. Faxbot checks that the document is a readable PDF and stores it. Only then is the fax **Received**, and only then can it be downloaded or [emailed](intake.md).

If Faxbot restarts during these steps, it carries on with the same fax. A notification the provider sends again continues the same fax and never creates a second one.

## Statuses

The Inbox, `faxbot received list` and the API's `status_text` show one sentence for each state.

| Status | Sentence | Meaning |
| --- | --- | --- |
| Waiting for the document | Waiting for the document from Phaxio. | Faxbot is fetching the document now. |
| Waiting for the document | The document could not be fetched; Faxbot will try again at 14:05. | The last attempt failed and Faxbot will try again on its own. |
| Not received | Faxbot stopped trying to fetch this document; select Fetch again. | Faxbot tried for about a day and stopped. |
| Received | (none) | The document is stored and can be downloaded and emailed. |
| Received | This fax arrived earlier and is kept as received. | The provider later sent different content for the same fax. Faxbot kept the first copy and recorded the conflict. |
| Test fax | (none) | A test fax created in Faxbot. |

After a failed attempt, Faxbot waits 1, 2, 4, 8, 16 and 32 minutes, then tries once an hour for 24 hours. In the API, `status` is `waiting`, `received` or `failed`, `retry_at` gives the time of the next attempt, and `can_fetch_again` says whether **Fetch again** applies.

While a fax is waiting, **Download PDF** stays disabled and the email delivery column shows **Waiting for the document**. Downloading through the API returns 404 with the message "The document has not been received yet."

## Fetch again

**Fetch again** asks Faxbot to try right away. It appears in the Inbox on waiting and not-received faxes, for people allowed to change provider settings (`providers:write`). Each request is recorded in the audit log.

From the command line, use the received fax's ID (`faxbot received list --ids` shows it):

```bash
faxbot received fetch <received fax ID>
```

The API equivalent is `POST /inbound/{id}/fetch`. A fax that has already been received answers 409.

A provider sending the same notification again also makes Faxbot try again, even after it had stopped.

## Test faxes

**Add Test Fax** in the Inbox, or `faxbot system diagnostics test-fax`, creates a one-page PDF that reads "Test fax created in Faxbot on" and the date. The Inbox marks it **Test fax**, the CLI shows `Test fax  yes`, and the API sets `is_test` to `true`. Email delivery sends a test fax like any other, so you can check mailboxes and email delivery end to end.

## How notifications are checked

### Phaxio

In Phaxio's receive callback settings, set the callback URL to `PUBLIC_API_URL` followed by `/phaxio-inbound`, exactly as Faxbot's public address is configured.

With signature checks on (`PHAXIO_INBOUND_VERIFY_SIGNATURE=true`, the default), every notification needs a valid `X-Phaxio-Signature` header. This is Phaxio's documented check: an HMAC-SHA1, keyed with the account's Callback Token (`PHAXIO_CALLBACK_TOKEN`), over the callback URL, the form fields sorted by name, and the SHA-1 digest of each attached file. Faxbot refuses the notification when the signature is wrong or the Callback Token is not set. If Phaxio attached the PDF (the `file` part), Faxbot uses it. Otherwise Faxbot downloads it from `https://api.phaxio.com/v2.1/faxes/{id}/file` with the configured API key and secret.

Earlier versions of Faxbot checked an HMAC-SHA256 of the request body made with the API secret. That is not Phaxio's scheme. Faxbot now checks Phaxio's signature as described above, and a callback is refused unless the Callback Token is set.

With signature checks off, a notification is only a hint. Before recording anything, Faxbot looks the fax up in the configured Phaxio account (`GET https://api.phaxio.com/v2.1/faxes/{id}`). Faxbot ignores the notification when it finds one of these:

- the account has no received fax with that ID
- the numbers in the notification disagree with Phaxio's record

An attached file is never used in this mode. Faxbot accepts at most 60 notifications a minute from one address while checks are off.

### Sinch

Set the incoming fax webhook of your Sinch fax service to `PUBLIC_API_URL` followed by `/sinch-inbound`.

When both `SINCH_INBOUND_BASIC_USER` and `SINCH_INBOUND_BASIC_PASS` are set, every notification has to match them. (Sinch Fax API v3 does not sign webhooks, so there is no signature to check.) Faxbot then uses a document attached to the notification, as base64 in JSON or as a multipart `file` part.

Otherwise, Faxbot first looks the fax up in the configured project (`GET /v3/projects/{projectId}/faxes/{id}`). It ignores faxes that project did not receive, and downloads the document from `/v3/projects/{projectId}/faxes/{id}/file`.

### Asterisk

The dialplan hands each received fax to `POST /_internal/asterisk/inbound` with the internal secret. You do not have to choose that secret: when receiving over the SIP trunk is possible and none is set, Faxbot creates one (saved as a setting by "system"), and it writes it to `/faxdata/asterisk/inbound.secret` (readable only by its owner) at every start and on **Apply and connect**, where Asterisk reads it. A value you set in Settings or as `ASTERISK_INBOUND_SECRET` in `.env` is used instead. The fax image must be inside Faxbot's data folder; Asterisk writes it to `/faxdata/inbound/`. Faxbot refuses paths outside that folder and paths that go through a symbolic link. A second report with the same call ID is the same fax.

A hand-over that fails is never silent. Asterisk logs an ERROR line naming the call, and Recent calls, **Check trunk status** and the Dashboard say "A fax was received but could not be handed to Faxbot:" with the reason (no inbound secret yet, the secret was refused, receiving is turned off, the image could not be read, or Faxbot could not be reached). The image stays in `/faxdata/inbound/`. Every minute Faxbot looks there for images that have no import yet and brings them in through the same path, keyed on the call ID, so a late hand-over and a recovery are one fax. It waits until the call has ended, or until the image has not changed for 10 minutes, so a fax that is still arriving is never imported half-way. A recovered fax is marked as recovered, and its source time is the image file's modification time; the Inbox and `faxbot received list` show that time with "brought in later". A number the call did not report shows as Unknown, except that a recovered fax's To number is the trunk's fax number when the trunk has exactly one (the import records that it was inferred). To check at once, select **Bring in faxes that were received but not handed over** under Recent calls on the trunk screen, or run `faxbot received recover`.

If Faxbot cannot convert the image to a PDF, the fax waits and the image stays in place. The next attempt starts again from that image.

The provided Docker setup shares one `/faxdata` folder between Asterisk and Faxbot, with `FAX_DATA_DIR=/faxdata`. If you run Faxbot with a different data folder, Asterisk's `/faxdata/inbound/` must be inside it. Otherwise Faxbot refuses every received image, and those faxes never appear in the Inbox even though the images stay on disk.

## What Faxbot never does

- It never requests an address a notification supplies, such as a `file_url` field.
- It only contacts the providers' documented API hosts:
    - `api.phaxio.com`
    - `fax.api.sinch.com`
    - `us.fax.api.sinch.com`
    - `eu.fax.api.sinch.com`
- It does not follow redirects. A request times out after 30 seconds, and a document over 50 MB is refused.
- It never shows or emails a placeholder in place of a fax.

## Where the document is stored

Faxbot keeps the document in the data folder as `<fax ID>-<first 12 characters of its SHA-256>.pdf`. When inbound storage is S3, it goes to S3. Received documents follow `INBOUND_RETENTION_DAYS` as before.

## Encoded documents

When a received fax carries encoded pages, Faxbot checks the recovered original's fingerprint and keeps the fax as received. **Faxes → Received** shows the decode result in the fax's details. Select **Download the original document**, or run `faxbot received decoded ID --output original.pdf`, to save a recovered original. Email delivery attaches it beside the fax as received.

If recovery fails, the details say why and email delivery includes the fax as received. After you change a stored shared key, reopen the fax's details or try the original download to check it again. For encrypted pages, the receiving Faxbot needs the same key stored against the sender's fax number. The current per-number control also enables encoded outbound faxes to that number, so use it only with that recipient's agreement.

Recovered originals stay in Faxbot's local data folder even when received fax PDFs use S3. They expire with the received fax. The decode result remains visible after expiration, but the original can no longer be downloaded.

## What is kept as evidence

For each received fax, Faxbot keeps:

- **Two times.** The time Faxbot recorded the fax (`received_at`; **Received** in the CLI). The time the provider reported the fax complete (`source_received_at`; **Sent** in the CLI). Faxbot stores the provider's time only when the provider reports one; it never fills it in with Faxbot's own time.
- **The fax's identity.** The provider's fax ID, and the account the notification was checked against. The same fax ID under two accounts is two separate faxes. The account is stored without secrets:
    - Phaxio: `phaxio:` and the first 12 hex digits of the SHA-256 of the API key
    - Sinch: `sinch:` and the project ID
    - Asterisk: `sip:` and the trunk user name
- **The document's details.** Its SHA-256 digest, size, page count and media type.
- **The notification.** A copy of at most 8 KB that records how it was checked. Signatures, credentials and file contents are left out.

Stored records are never rewritten to change a received document.

Earlier versions stored a fixed stand-in PDF when a document could not be fetched, and for test faxes. Faxbot now shows those faxes as **Not received**. It does not offer them for download or email, and it leaves the stored rows as they are.

## Not yet checked against live providers

These behaviours are covered by synthetic tests only. No live Phaxio or Sinch notification has been received with this version.

- If Phaxio's file download answers with a redirect, Faxbot records it as a failed fetch, because it does not follow redirects.
- If the Phaxio API key or Sinch project changes after a fax arrived, Faxbot cannot fetch that fax with the new account and says so.
