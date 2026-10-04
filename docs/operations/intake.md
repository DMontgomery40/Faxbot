# Intake

Every document Faxbot receives, by fax or by [direct delivery](direct-delivery.md), goes into one intake queue. Faxbot then delivers each one to where your staff already look. Today that is an email inbox, with the original PDF attached.

In the Admin Console, the **Inbox** shows each received fax with its email delivery, and **Settings** holds the email delivery setup. Select **Email delivery settings** at the top of the Inbox to go there.

Email delivery is not acknowledgement: to give each received document an owner who acknowledges it, see [Work](work.md).

## Delivery status in the Inbox

Each received fax shows one of these in its **Email delivery** column (a line on each card on phones):

| Status | Meaning |
| --- | --- |
| Delivered to (addresses) | The email server accepted it, at the time shown. |
| Waiting for email delivery | Faxbot will deliver it shortly, or it is waiting for you to send it. |
| Not delivered | Faxbot could not deliver it. The line below says why. |
| No email delivery set up for this number | No email delivery covered the number the fax was sent to when it arrived. After setting one up, select **Retry delivery**. |
| - | An older fax with no delivery record, such as one received without a document. |

Documents received by direct delivery have no fax record; the Inbox lists them under **Received by direct delivery**.

A document is delivered at most once on its own. **Retry delivery** sends a waiting or undelivered document again; use it after fixing the problem the status describes. It needs permission to change settings.

People who cannot read the installation's deliveries (`mailboxes:read`) see the Inbox without the delivery column.

## Set up email delivery

In **Settings**, under **Email delivery**:

1. Select **Add email delivery**.
2. Enter a name, the recipient addresses, and your email server, port, security and sign-in details.
3. Enter the address faxes are sent from, and a subject. The subject can include `{from_number}`, `{to_number}`, `{pages}` and `{received_at}`.
4. To deliver only faxes sent to one of your numbers, enter that number. Leave it empty to deliver faxes for every number.
5. Save, then select **Send test email** to check the settings.

Each email carries one sentence describing the fax, such as "Fax from +1 555 010 9999 to +1 555 010 0001, 3 pages, received 3 October 2026 at 14:05 UTC.", and the original PDF as an attachment.

When a fax number has its own email delivery, Faxbot uses it. Otherwise it uses the email delivery with no number.

Passwords are encrypted with the installation's configuration key and are never shown again.

Email delivery handles faxes that arrive after it is set up. Faxes received earlier show **Waiting for email delivery**. Send them with **Retry delivery** when you are ready.

## Intake defaults

One email delivery can be defined by the installation settings, in **Settings → Intake defaults** (or the environment when a new installation starts). Faxbot keeps it in step with the settings within a few minutes, and shows it under **Email delivery** as set by Intake defaults:

```env
INTAKE_EMAIL_ENABLED=true
INTAKE_SMTP_HOST=smtp.example.org
INTAKE_SMTP_PORT=587
INTAKE_SMTP_SECURITY=starttls   # starttls, tls or none
INTAKE_SMTP_USERNAME=fax@example.org
INTAKE_SMTP_PASSWORD=...
INTAKE_EMAIL_FROM=fax@example.org
INTAKE_EMAIL_TO=frontdesk@example.org, billing@example.org
INTAKE_EMAIL_SUBJECT=Fax from {from_number}
```

## When delivery fails

- When the email server refuses before the message is sent, nothing was delivered. If the refusal may be temporary, Faxbot tries again after 1, 2, 4, 8 and 16 minutes, then stops and marks the document **Not delivered**.
- When the email server refuses the sign-in or the addresses, Faxbot stops at once and says so.
- When the connection drops after the message was sent, the email may have arrived. Faxbot marks the document **Not delivered** and asks you to check the inbox before sending it again. It never resends on its own.
- Each email has the same Message-ID every time it is sent, so many mail systems recognize a second copy.

## API

These routes need `mailboxes:read`, or `settings:write` for changes:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/intake/items` | The queue, with counts by status; each item names its inbound fax (`inbound_fax_id`) and, once delivered, the addresses it went to (`delivered_to`) |
| POST | `/intake/items/{id}/retry` | Send one document now |
| GET, POST | `/intake/connectors` | List or add email delivery |
| PUT, DELETE | `/intake/connectors/{id}` | Change or remove email delivery |
| POST | `/intake/connectors/{id}/test` | Send a one-line test email |

The queue is an installation-wide view of every received document, so reading it needs `mailboxes:read`.
