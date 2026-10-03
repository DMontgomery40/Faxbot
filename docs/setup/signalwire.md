
# SignalWire Compatibility API

SignalWire’s “Compatibility” endpoints mimic the old Twilio Fax API. Faxbot uses a tokenised MediaUrl so SignalWire can fetch documents securely.

## Gather before you start

- Space URL (e.g., `example.signalwire.com`)
- Project ID and API Token
- Public HTTPS URL for callbacks and PDF fetches

## Configure with the Setup Wizard

1. Admin Console → **Setup Wizard**
2. Choose **SignalWire Compatibility**
3. Enter Space URL, Project ID, API Token, and default From number
4. Provide your public URL; the wizard computes the MediaUrl that SignalWire fetches
5. Apply the changed desired fields and inspect active/pending status. Pending changes require every API worker to stop and the installation restart. Existing `.env` edits do not import configuration.

{: .note }
Need a quick tunnel? Follow [Public Access & Tunnels](public-access.md).

## Send & monitor

- **Send Fax** durably accepts PDF/TXT and records ready or held work. Held work has no issued attempt and never automatically transmits when sending is enabled. A dispatched attempt supplies its captured tokenized PDF URL.
- SignalWire fetches the MediaUrl via HTTPS
- Status callbacks hit `/signalwire-callback` with captured job/attempt locators. Set the separate webhook signing key in Settings; unsigned or mismatched callbacks are rejected. The original account and remote fax ID must match.
- Watch status transitions in **Jobs** with provider-specific troubleshooting links

## How it works (under the hood)
- Faxbot mints a short‑TTL tokenised MediaUrl and includes it in the create‑fax request
- SignalWire fetches the MediaUrl via HTTPS; Faxbot returns the PDF if and only if token and TTL match
- Compatibility form Scheme B callbacks require `X-SignalWire-Signature`, verified with the captured signing key and submitted public URL/form values. Verification is not optional authority for an unsigned update.
- Admin coverage: Diagnostics shows callback URL and signature settings; Jobs reveal provider SID

## Troubleshooting

- **403 fetching MediaUrl** → Check the captured token, expiry and URL for that attempt; reopening Jobs does not mint a replacement provider link or resubmit.
- **401 from SignalWire** → Regenerate the API token in the SignalWire console and rerun the wizard
- **No callbacks** → Check the captured URL/query, signing key and original account. Supported polling uses that captured account; uncertain outcomes require reconciliation before any new transmission.

## References

- SignalWire Fax docs: <https://developer.signalwire.com/>
- Compatibility API overview: <https://developer.signalwire.com/apis/docs/fax>
