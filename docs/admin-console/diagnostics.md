# Diagnostics

**System → Diagnostics** answers one question: is Faxbot working, and if not, what do you do about it? Each check is one sentence, and a check that needs you has a button that opens the page where you fix it.

The page shows the last results as soon as it opens. The first visit after Faxbot starts runs the checks by itself; after that, select **Check now**. Checks only read. No fax is sent, no setting changes, and nothing costs money.

## What is checked

| Section | Checks |
| --- | --- |
| Sending | The sending provider accepts Faxbot's sign-in: HumbleFax through its read-only account lookup, eFax through its sign-in. Faxes that may or may not have arrived and wait for you to confirm them in Sent. The last delivered fax and the last 7 days. |
| Receiving | The receiving provider's sign-in (when it is not the sending account). Received faxes Faxbot stopped trying to fetch. The last fax received. Each email delivery: Faxbot signs in to the email server and leaves without sending a message. Faxes that could not be emailed. |
| Fax engine | Whether Faxbot's own fax engine is running and how many calls are up. For a carrier trunk: whether the carrier accepted Faxbot's sign-in and answers its checks, whether received faxes reach Faxbot, why fax over IP (T.38) is off when Faxbot turned it off, Faxbot's internet address and the last call. |
| This server | The database, free disk space where faxes are kept (a warning below 2 GB or 5%), whether Faxbot can write fax files, the document converter, the time zone times are shown in, settings waiting for a restart, and online storage when it is used. |
| Security | The audit log, request limits and secure links for fax services. |

A provider without a read-only sign-in check (Phaxio, Sinch, SignalWire, Documo) shows that its sign-in details are saved; the next fax shows whether the provider accepts them. A check that cannot finish says so and does not hide the others.

The summary at the top counts what is not working and what needs attention. **Copy results** and **Download** give the same sentences as plain text, without passwords, keys or provider replies.

## Other actions

- **Restart Faxbot** asks Faxbot to stop, only when the installation allows it (System → Diagnostics → **Allow restarting Faxbot from here**). With Docker Compose the `api` service starts again by itself, usually within a few seconds; elsewhere your process manager must start it.
- **Read saved settings again** asks Faxbot to read its saved settings (`POST /admin/settings/reload`). It never applies changes that wait for a restart.
- The **Database** card shows what kind of database Faxbot uses, whether it can reach it and what you can see.
- **Also check the S3 bucket** adds a reach check of the online storage bucket to the settings check below.

## Command line

```
faxbot system diagnostics run      # check now
faxbot system diagnostics show     # the last results, without checking again
faxbot system diagnostics engine registrations|contacts|calls|faxes
```

## Related endpoints

- `POST /admin/diagnostics/report`: run every check now. `GET` returns the last run without contacting anything.
- `GET /admin/diagnostics/engine/{view}`: what the fax engine reports (System → Developer → Scripts & checks).
- `POST /admin/diagnostics/run`: every saved setting as a value, for older clients and scripts.
- `GET /health/ready`: the shared readiness check the Overview uses.
- `GET /admin/db-status`: the database card.
- `POST /admin/restart`: the optional restart request.
