
# SignalWire Compatibility API

SignalWire’s “Compatibility” endpoints mimic the old Twilio Fax API. Faxbot uses a tokenised MediaUrl so SignalWire can fetch documents securely.

## Gather before you start

- Space URL (e.g., `example.signalwire.com`)
- Project ID and API Token
- Public HTTPS URL for callbacks and PDF fetches

## Configure Faxbot

1. In the Admin Console, open **System → Setup** and choose SignalWire.
2. Enter the Space URL, Project ID, API Token, and the number to send faxes from.
3. Apply the changes. Faxbot uses a tokenized PDF URL when it submits a fax; SignalWire must be able to fetch that URL over HTTPS.

To review or change these settings later, open **Providers → SignalWire**. The optional status update address and signing key are there too. Leave the address empty to use Faxbot's public address. If you change settings that require a restart, stop all API workers and restart the installation.

{: .note }
Need a quick tunnel? Follow [Public Access & Tunnels](public-access.md).

## Send & monitor

- **Send Fax** durably accepts PDF/TXT and records ready or held work. Held work has no issued attempt and never automatically transmits when sending is enabled. A dispatched attempt supplies its captured tokenized PDF URL.
- SignalWire fetches the MediaUrl via HTTPS
- Status callbacks hit `/signalwire-callback` with captured job/attempt locators. Set the separate webhook signing key in Settings; unsigned or mismatched callbacks are rejected. The original account and remote fax ID must match.
- Watch status transitions in **Faxes → Sent** with provider-specific troubleshooting links

## How it works (under the hood)
- Faxbot mints a short‑TTL tokenised MediaUrl and includes it in the create‑fax request
- SignalWire fetches the MediaUrl via HTTPS; Faxbot returns the PDF if and only if token and TTL match
- Compatibility form Scheme B callbacks require `X-SignalWire-Signature`, verified with the captured signing key and submitted public URL/form values. Verification is not optional authority for an unsigned update.
- Admin coverage: Diagnostics shows callback URL and signature settings; Sent's fax details show the provider SID

## Troubleshooting

- **403 fetching MediaUrl** → Check the captured token, expiry and URL for that attempt; reopening the fax in Sent does not mint a replacement provider link or resubmit.
- **401 from SignalWire** → Regenerate the API token in the SignalWire console and rerun the wizard
- **No callbacks** → Check the captured URL/query, signing key and original account. Supported polling uses that captured account; uncertain outcomes require reconciliation before any new transmission.

## References

- SignalWire Fax docs: <https://developer.signalwire.com/>
- Compatibility API overview: <https://developer.signalwire.com/apis/docs/fax>
