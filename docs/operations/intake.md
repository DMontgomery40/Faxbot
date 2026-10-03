# Intake

Every document Faxbot receives, by fax or by [direct delivery](direct-delivery.md), goes into one intake queue. Faxbot then delivers each one to where your staff already look. Today that is an email inbox, with the original PDF attached.

Open **Tools → Intake** in the Admin Console.

## The queue

Each received document appears once, with a status:

| Status | Meaning |
| --- | --- |
| Waiting | Faxbot will deliver it, or it is waiting for you to send it. |
| Sending | Faxbot is delivering it now. |
| Delivered | The email server accepted it. |
| Not delivered | Faxbot could not deliver it. The status line says why. |

A document is delivered at most once on its own. **Send now** delivers a waiting or undelivered document again; use it after fixing the problem the status describes.

## Set up email delivery

1. Select **Add email delivery**.
2. Enter a name, the recipient addresses, and your email server, port, security and sign-in details.
3. Enter the address faxes are sent from, and a subject. The subject can include `{from_number}`, `{to_number}`, `{pages}` and `{received_at}`.
4. To deliver only faxes sent to one of your numbers, enter that number. Leave it empty to deliver faxes for every number.
5. Save, then select **Send test email** to check the settings.

Each email carries one sentence describing the fax, such as "Fax from +1 555 010 9999 to +1 555 010 0001, 3 pages, received 3 October 2026 at 14:05 UTC.", and the original PDF as an attachment.

When a fax number has its own email delivery, Faxbot uses it. Otherwise it uses the email delivery with no number.

Passwords are encrypted with the installation's configuration key and are never shown again.

Email delivery handles faxes that arrive after it is set up. Faxes received earlier stay in the queue as **Waiting**. Send them with **Send now** when you are ready.

## Settings

You can define one email delivery in the installation settings instead of the console. Faxbot keeps it in step with the settings and shows it as read-only:

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
| GET | `/intake/items` | The queue, with counts by status |
| POST | `/intake/items/{id}/retry` | Send one document now |
| GET, POST | `/intake/connectors` | List or add email delivery |
| PUT, DELETE | `/intake/connectors/{id}` | Change or remove email delivery |
| POST | `/intake/connectors/{id}/test` | Send a one-line test email |

The queue is an installation-wide view of every received document, so reading it needs `mailboxes:read`.
