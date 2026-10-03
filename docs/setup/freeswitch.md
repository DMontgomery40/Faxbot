
# FreeSWITCH Fax (Self-Hosted)

Use this backend when your telephony stack is already built on FreeSWITCH and you want Faxbot to orchestrate fax jobs via `txfax`.

## Prerequisites

- FreeSWITCH with `mod_spandsp` enabled
- Working SIP gateway (`sofia/gateway/<name>`) that supports T.38
- Network path from Faxbot → FreeSWITCH (often inside the same LAN or docker network)
- Admin access to your dialplan to add the completion hook

## Configure Faxbot

1. Admin Console → **Setup Wizard**
2. Choose **FreeSWITCH**
3. Fill the gateway name, caller ID, and any authentication required
4. Apply the changed desired fields with the loaded revision and inspect active/pending status. Complete a coordinated installation restart when pending. Configure ESL and the result hook in the FreeSWITCH deployment; Setup does not generate or verify the hook.

## Completion hook contract

The native originate helper passes `faxbot_job_id` and `faxbot_attempt_id` to the channel and calls `&txfax(...)` directly. A snippet placed only in a separate outbound dialplan context is not enough to install a completion hook on that direct application path. Configure the hook on the actual originated channel and verify it in your FreeSWITCH deployment before live use; Setup does not install it.

The private `/_internal/freeswitch/outbound_result` route expects a mapped terminal status and both captured identifiers, for example:

```json
{
  "job_id": "<faxbot_job_id>",
  "attempt_id": "<faxbot_attempt_id>",
  "fax_status": "success"
}
```

Send the configured internal secret as `X-Internal-Secret`, matching the accepted API revision's `ASTERISK_INBOUND_SECRET`. Map an actual terminal fax result to `success`, `failed` or `cancelled`; do not turn an originate acknowledgement, missing result or held job into success. Avoid interpolating arbitrary provider text into a shell/JSON command.

Faxbot durably accepts ready or held jobs and prepares real TIFF artifacts when required. Only a claimed normal attempt triggers `bgapi originate ... &txfax(...)`. Its Job-UUID is a submission acknowledgement, not delivery. Held, unissued, mismatched or unauthenticated result updates are refused. An absent/uncertain result requires checking the original FreeSWITCH attempt before another call. Real transport/hook/document delivery remains an operational check.

## Security notes

- Keep `fs_cli`/ESL access restricted to a private network
- Use TLS or a VPN to reach your SIP gateway whenever the carrier supports it
- Disable any FreeSWITCH document storage to avoid retaining PHI longer than necessary

## Troubleshooting

- **Hook never fires** → Confirm the hook is installed on the direct originate channel, then review its quoting and private route credentials. A dialplan action in an unused context will not run.
- **Jobs in progress or reconciliation required** → Check the original FreeSWITCH attempt, hook job/attempt IDs, secret and endpoint. Do not originate another call solely because the result is missing.
- **TIFF missing** → Check Faxbot API logs for Ghostscript conversion output.

More FreeSWITCH context lives in [Faxbot third-party references](../third-party.md).
