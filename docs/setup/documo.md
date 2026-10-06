# Documo (mFax)

The builtin Documo adapter uploads prepared PDFs directly and polls the original account for status. A public document URL is not needed for outbound sending. Submission acknowledgement is separate from delivery.

## Configure the installation

1. Open the **Setup Wizard** (**System → Setup**), or **Providers → Documo** when it is already in use.
2. Select **Documo (mFax)** for outbound and enter the API key for the intended account. Leave unchanged secret masks alone.
3. Choose production or sandbox. In Settings, review **Documo Base URL** using the rules below.
4. Apply the changes. If Faxbot asks for a restart, stop every API process and start the installation again, then confirm that no restart is pending.
5. Use **Faxes → Send a fax** with a synthetic document and a controlled destination. Disabled sending creates permanently held jobs; real transmission requires sending to be enabled. Inspect **Faxes → Sent** and the original provider account for the result and document fidelity.

Setup does not authenticate a Documo key. Readiness checks local configuration, not account access or delivery. The key must permit both sending and reading fax information; a create-only key cannot support status polling. When a new installation starts for the first time, it can read `FAX_OUTBOUND_BACKEND=documo`, `DOCUMO_API_KEY`, `DOCUMO_BASE_URL` and `DOCUMO_SANDBOX` from the environment. After that, change them in Settings; later `.env` edits are not imported.

## Endpoint and sandbox rules

- Production defaults to `https://api.documo.com`.
- Sandbox enabled with a blank/default production base selects `https://api.sandbox.documo.com`. The explicit sandbox root is also accepted. A different override is rejected rather than risking production transmission under a sandbox label.
- A production override must be an HTTPS origin, optionally with a port and trailing slash. Do not append `/v1`, a custom path, credentials, query or fragment. The adapter appends the documented API paths and does not follow redirects or try another host.
- Sandbox account provisioning is separate from selecting the endpoint. Documo describes sandbox as non-transmitting; this is provider behavior, not Faxbot's local held mode. See [Documo's sandbox guide](https://help.documo.com/hc/en-us/articles/7789817420571-Sandbox-Environment).

## Delivery and recovery

The adapter sends one multipart `POST /v1/faxes` with `faxNumber`, the PDF attachment, `coverPage=false` and `async=true`. Authentication uses Documo's literal `Authorization: Basic <API key>` format. It omits caller ID, so the provider account's default applies. Faxbot does not claim to freeze remote account preferences.

A returned `messageId` identifies the original provider fax. An ID-only asynchronous acknowledgement or `processing` means in progress. Polling reads `/v1/fax/{messageId}/info` using the accepted account and requires the matching ID. Documented `success` and `failed` are terminal; missing, malformed or unknown results do not manufacture success.

An ambiguous create is never automatically resubmitted. Check the original account first. For an issued unresolved attempt without an ID, **Confirm receipt** in the fax's details (**Faxes → Sent**) can attach the confirmed Documo UUID with explicit operator confirmation, then resume original-account polling. This does not send again or mark the fax delivered. Later credential/provider changes do not move accepted work to a different account.

Documo cancellation and callback mutation are not implemented by this adapter. Do not infer a provider cancellation from a local request. See [held test jobs](test-mode.md) for document checks without creating provider attempts.

## Configuration and troubleshooting

- Faxbot stores the credentials encrypted in its database. The exported `.env` template hides them and is not a backup.
- Provider document retention and caller ID defaults are account settings, not controls enforced by this panel.
- An authentication or polling failure requires checking the original account's key permissions. Editing current credentials does not replace an accepted attempt's captured account.
- A rejected endpoint requires an HTTPS origin without `/v1` or another path; a sandbox override must follow the rules above.
- Read failures and rate limits preserve uncertainty. Inspect the original fax before considering another submission.

## References and verification

The adapter follows the [official Documo API documentation](https://docs.documo.com/). Protocol and captured-account tests are separate from operational verification: validate account access, controlled delivery and received page/content fidelity before relying on a deployment.
