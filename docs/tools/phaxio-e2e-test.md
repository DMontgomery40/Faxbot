# Phaxio End-to-End Delivery Check

This check sends a real fax through Phaxio to a controlled Phaxio receiving number. No physical fax machine is required, but it is a provider transmission and may incur account charges. A held job or accepted request is not a completed delivery check.

## Prerequisites

- Phaxio account API Key and API Secret, plus its separate account Callback Token for authenticated outbound callbacks.
- A receiving number you control in the Phaxio account.
- A reachable HTTPS URL for provider PDF fetching and callbacks.
- A synthetic document with identifiable content; access to Jobs and the original provider account's result/document.

## 1. Establish HTTPS reachability

Start your chosen tunnel or use the installation's existing HTTPS endpoint. For a temporary local tunnel, run one of these and retain the displayed HTTPS URL:

```sh
cloudflared tunnel --url http://localhost:8080
# or
ngrok http 8080
```

The legacy `scripts/setup-phaxio-tunnel.sh` starts a tunnel, edits repository `.env` and stops/restarts Compose. Those edits are bootstrap inputs; the helper does not update canonical desired settings on an existing installation. Use the manual tunnel plus Settings steps below for an existing installation. This guide does not claim the helper has been repaired.

## 2. Configure the desired revision

1. Open **Settings** or **Setup Wizard** and load the current desired revision.
2. Select Phaxio for the outbound direction. Review the default and independent inbound choice; do not change inbound handling merely to choose an outbound provider.
3. Set the Phaxio API Key, API Secret and separate Callback Token. Leave unchanged stored masks alone.
4. Set **Public API URL** to the HTTPS URL. Set **Status Callback URL** to its `/phaxio-callback` endpoint, or leave it empty to derive that URL. Enable **Verify outbound status signatures** for callback updates.
5. Apply changes and inspect active/desired status. For pending fields, stop every API worker and restart the installation, then confirm the desired revision is active.
6. Configure authentication and use the current Faxbot client key. For a real transmission, sending must be enabled in the active configuration. Previously held jobs remain held when it is re-enabled.

For first bootstrap only, the corresponding environment names are `FAX_BACKEND`, `FAX_OUTBOUND_BACKEND`, `PHAXIO_API_KEY`, `PHAXIO_API_SECRET`, `PHAXIO_CALLBACK_TOKEN`, `PHAXIO_STATUS_CALLBACK_URL`, `PHAXIO_VERIFY_SIGNATURE` and `PUBLIC_API_URL`. Editing `.env` after canonical initialization is not a settings import.

## 3. Submit one controlled document

In **Send**, attach the synthetic PDF/TXT and enter the receiving number. Submit once and retain the returned job ID. In **Jobs**, inspect the prepared document, delivery state and issued attempt. A 202 response records acceptance, not a provider receipt or delivery success.

API clients can submit the same controlled document using their current key:

```sh
curl -X POST http://localhost:8080/fax   -H "X-API-Key: $API_KEY"   -F to=+1YOURPHAXIONUMBER   -F file=@./synthetic.pdf
```

The legacy `send-fax.sh` and `get-status.sh` read the header key from repository `.env`; shell `API_KEY` alone is not forwarded by those helpers. Use the current client key explicitly as above or use the Admin Console.

## 4. Verify the actual outcome

- Inspect **Jobs** and the original Phaxio account for the same remote fax ID and terminal result. A provider submission acknowledgement is not delivery.
- Inspect the receiving account's delivered document and page/content fidelity, not only a status label.
- Outbound callbacks must authenticate the captured account, job/attempt locators, remote fax ID and signature using the separate Callback Token. See [callback verification](../setup/webhooks.md#outbound-status-phaxio).
- Original-account polling can update status when the captured provider fax ID is known; a later credential/provider edit does not move this attempt to another account.
- If outcome is uncertain or `reconciliation_required`, check the original account before taking action. Do not submit another fax merely because a callback, status read or request response failed.

This verifies outbound transmission and receipt in the provider's receiving account. It does not verify Faxbot's unfinished inbound ingestion/document workflow.
