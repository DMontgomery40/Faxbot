# API Reference

The [generated source reference](generated/index.md) supplies the current OpenAPI contract and source provenance. This guide explains acceptance, captured configuration and delivery outcomes for operators. Faxbot's API runs on your self-hosted installation; the public website, simulated demo and static API-reference documentation on `faxbot.net` are separate from your server.

## Base URL and authentication

Use your installation's API URL; the local default is `http://localhost:8080`. Every request needs a credential: send a Faxbot API key as `X-API-Key`. What the key may do depends on its owner's roles and the key's own permission list. Editing the `.env` file does not change keys on an existing installation. See [Authentication](security/authentication.md) and [Access Control](security/access-control.md).

Open `/docs` on that server for its Swagger UI (`http://localhost:8080/docs` in local development); `/openapi.json` provides its schema. Remote clients, including iOS, connect to your installation through its configured secure tunnel or VPN. See [Public Access & Tunnels](setup/public-access.md).

## Submit a document

`POST /fax` accepts multipart `to` and `file` fields. `to` is a number with its country code, starting with `+`, or a national number for the installation country (`FAX_DEFAULT_COUNTRY`, default US); Faxbot stores and returns it in E.164 and refuses an incomplete number. Requests with the same `Idempotency-Key` are the same request when their numbers resolve to the same E.164 number with the same document and queue setting; faxes accepted before this release replay only with their original `to` text. A final line break in a TXT file does not add a blank page, and one-bit TIFF pages stay one-bit in the generated PDF. Supported PDF, TXT and TIFF preparation preserves document contents and validates the active upload limit. The optional `queue_only=true` condition refuses a stale queue-only form when sending has become enabled. The optional `send_now=true` sends at once a fax to a number that [sends faxes together](operations/delivery-routes.md#sending-together); faxes already waiting for that number go in the same call.

```sh
curl -X POST http://localhost:8080/fax   -H "X-API-Key: $API_KEY"   -F to=+15551234567   -F file=@./synthetic.pdf
```

A 202 response records durable acceptance, with a job ID, compatibility status and delivery metadata. It is not a provider submission acknowledgement or terminal delivery. New jobs accepted with sending disabled are held with no issued attempt and never automatically transmit after re-enabling. Normal acceptance records ready work for the dispatcher.

Typical refusals include invalid number/document, unsupported type, oversized upload, authentication/scope failure and a conflicting queue-only request. See generated OpenAPI for exact models and response codes. If an outcome is uncertain, retain any returned job ID and inspect the original record/provider before submitting another request.

## Read job state

`GET /fax/{job_id}` returns the stored job and delivery metadata; it does not transmit or retry it. Read with a key permitted for fax status:

```sh
curl -H "X-API-Key: $API_KEY" http://localhost:8080/fax/$JOB_ID
```

Relevant fields include `id`, `to`, `status`, `pages`, captured `backend`, `provider_sid`, timestamps, `delivery_state`, `dispatch_mode`, `delivery_version` and `reconciliation_reason`. Use delivery state and attempt evidence, not a compatibility status label alone, to distinguish held/ready work, preparing/submitting, in-progress, terminal outcomes and reconciliation required.

Acceptance captures provider/account credentials, manifest/traits, URLs and configuration identity. Later settings changes do not move an existing attempt to a new account. Supported polling and authenticated callbacks observe that original attempt without resubmitting. Historical rows without a verified transmission record require reconciliation rather than automatic send. An uncertain provider response, timeout or missing callback is not permission to retry transmission blindly.

## Provider PDF access

`GET /fax/{job_id}/pdf?token=...` serves the prepared PDF using the accepted token and expiry. It has no API-key header requirement for provider fetching, but rejects an invalid/expired token. A tokenized URL is sensitive. Reopening a job does not mint a new provider URL or retry its attempt. Admin Jobs offers a separate authenticated PDF download.

Document conversion never substitutes a placeholder for a missing dependency or disabled sending. Required TIFF conversion fails honestly when Ghostscript is unavailable. Builtin Phaxio/Sinch/SignalWire use PDF paths; captured traits determine whether another provider requires TIFF.

## Outbound Phaxio callbacks

`POST /phaxio-callback` expects signed form fields and the captured `job_id`/`attempt_id` query locators. The captured account, attempt and provider fax ID must also match. An authenticated observation returns `{ "ok": true, "applied": ... }`.

`X-Phaxio-Signature` uses the separate `PHAXIO_CALLBACK_TOKEN`: lowercase hexadecimal HMAC-SHA1 over the exact captured URL/query, stably name-sorted form fields and file-part SHA1 digests. See [outbound callback verification](setup/webhooks.md#outbound-status-phaxio).

Missing/invalid authentication or captured `PHAXIO_VERIFY_SIGNATURE=false` rejects callback updates. Disabling verification does not authorize unsigned updates; polling continues with the captured original account when its provider fax ID is known. Inbound handlers are separate and are not certified by this outbound contract.

## Configuration, retention and logs

Change server settings on the [Settings](admin-console/settings.md) screen or its API. Environment files only apply when a new installation starts for the first time. When a change waits for a restart, stop every API process and start the installation again.

PDF link lifetime and document cleanup are ordinary settings. Keep artifacts needed for reconciliation and recovery; changing cleanup configuration is distinct from proving provider delivery. Audit logs can record events such as `job_created` and `pdf_served`; a PDF fetch or log event is not terminal fax delivery. Inspect durable job/attempt state and the original provider result.
