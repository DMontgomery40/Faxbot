# HumbleFax

The builtin HumbleFax adapter uploads prepared PDFs directly and polls the original account for status. It sends faxes only; it does not receive them. A public document URL is not needed. Submission acknowledgement is separate from delivery.

## Create API keys

1. Sign in to HumbleFax and open **Developer Settings**.
2. Create an API key pair. Copy both the **access key** and the **secret key** and store them in a password manager.
3. Note which of the account's fax numbers should appear as the sender. Leave the sender blank to use the number HumbleFax assigns by default.

## Configure the installation

1. Open **Settings** or **Setup Wizard** and load the current desired revision.
2. Select **HumbleFax** as the outbound provider.
3. Paste the access key and secret key. Leave unchanged secret masks alone.
4. Optional: enter **HumbleFax From Number** as 10 digits, or 11 digits starting with `1` (for example `13035550199`). It must be a fax number on the same HumbleFax account.
5. Apply the changed fields with the loaded revision. If the change is pending, stop every API worker and restart the installation; confirm that the desired revision is active.
6. Use **Send** with a synthetic document and a controlled destination. Disabled sending creates permanently held jobs; real transmission requires sending to be enabled. Inspect **Jobs** and the HumbleFax sent history for the result and document fidelity.

Setup does not authenticate HumbleFax keys. Readiness checks local configuration, not account access or delivery. The key pair must permit both sending faxes and reading sent fax details. For first bootstrap, the corresponding environment names are `FAX_OUTBOUND_BACKEND=humblefax`, `HUMBLEFAX_ACCESS_KEY`, `HUMBLEFAX_SECRET_KEY` and `HUMBLEFAX_FROM_NUMBER`. Existing installations use canonical Settings, not later `.env` edits.

## Destinations

HumbleFax sends to US and Canadian fax numbers. Faxbot accepts a North American number written as `+1` followed by 10 digits, 11 digits starting with `1`, or 10 digits. Any other destination fails before submission and HumbleFax is not contacted.

## Delivery and recovery

The adapter sends one multipart `POST /quickSendFax` to `https://api.humblefax.com` using HTTP Basic authentication (access key as user name, secret key as password). The request contains the PDF as `document.pdf`, the destination, `includeCoversheet: false`, `Fine` resolution, `Letter` page size, the optional sender number, and the Faxbot attempt identifier in HumbleFax's `uuid` field.

The returned fax ID identifies the original HumbleFax fax. Polling reads `GET /sentFax/{id}` using the accepted account and requires the matching ID. HumbleFax summary statuses map as follows:

| HumbleFax status | Faxbot status |
| --- | --- |
| `in progress`, `scheduled` | in progress |
| `success` | success |
| `failure`, `image failure`, `partial success` | failed |
| `cancelled` | cancelled |

Missing, malformed or unknown results do not manufacture success. `partial success` is reported as failed because not every page or recipient completed.

An ambiguous create is never automatically resubmitted. A timeout, rate limit, rejected reply or unreadable acknowledgement leaves the job requiring reconciliation. Check the original account's sent history first. For an issued unresolved attempt without an ID, **Jobs → Job Details** can attach the confirmed HumbleFax fax ID with explicit operator confirmation, then resume original-account polling. This does not send again or mark the fax delivered. Later credential or provider changes do not move accepted work to a different account.

HumbleFax cancellation, webhooks and inbound faxes are not implemented by this adapter. Do not infer a provider cancellation from a local request. See [held test jobs](test-mode.md) for document checks without creating provider attempts.

## Configuration and troubleshooting

- Credentials are captured in encrypted canonical configuration; a redacted template is not a recovery backup.
- When HumbleFax rejects the key pair, it creates no fax and Faxbot does not resend. The job still shows that it requires reconciliation. Recreate or recopy both keys from **Developer Settings**, apply them in Settings, then send a new fax. Editing current credentials does not replace an accepted attempt's captured account.
- A sender number that is not on the account is rejected by HumbleFax. Clear **HumbleFax From Number** to use the account default.
- HumbleFax documents a limit of 5 requests per second per IP address and blocks an address for 60 seconds when it is exceeded. Read failures and rate limits preserve uncertainty; inspect the original fax before considering another submission.
- Document retention, caller name and account notifications are HumbleFax account settings, not controls enforced by this panel. Faxbot always asks HumbleFax not to add a cover sheet.
- Confirm HumbleFax's compliance terms before sending protected health information. See [HIPAA requirements](../HIPAA_REQUIREMENTS.md).

## References and verification

The adapter follows the [official HumbleFax API documentation](https://api.humblefax.com/). Protocol and captured-account tests are separate from operational verification: validate account access, controlled delivery and received page/content fidelity before relying on a deployment.
