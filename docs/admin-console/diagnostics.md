# Diagnostics

**System → Diagnostics** answers one question: does Faxbot work, and if not, what do you do? Each check gives one sentence. When a check needs you, it has a button that opens the page where you fix the problem.

The page shows the last results when it opens. The first time you open it after Faxbot starts, it runs the checks by itself. After that, select **Check now**.

The checks only read. They send no fax, change no setting and cost no money.

## What is checked

| Section | Checks |
| --- | --- |
| Sending | Whether the sending provider accepts Faxbot's sign-in details. For HumbleFax, Faxbot uses its read-only account lookup. For eFax, Faxbot signs in. Faxes that may or may not have arrived, which you confirm in **Sent**. The last delivered fax and the last 7 days. |
| Receiving | Whether the receiving provider accepts Faxbot's sign-in details, when it is not the sending provider. Received faxes that Faxbot stopped trying to fetch. The last received fax. Each email delivery: Faxbot signs in to the email server and leaves without sending a message. Faxes that Faxbot could not email. |
| Fax engine | Whether Faxbot's own fax engine runs, and how many calls are in progress. For a carrier trunk, also: whether the carrier accepts Faxbot's sign-in details and answers its checks. Whether received faxes reach Faxbot. Why fax over IP (T.38) is off, when Faxbot turned it off. Whether the network allows fax over IP (T.38) ("Faxing over the internet"). The last call. |
| This server | The database. Free disk space where faxes are kept, with a warning below 2 GB or 5%. Whether Faxbot can write fax files. The document converter. The time zone that times are shown in. Settings that wait for a restart. Online storage, when it is used. |
| Security | The audit log, request limits and secure links for fax services. |

Some providers have no read-only sign-in check: Phaxio, Sinch, SignalWire and Documo. For them, the check only shows that the sign-in details are saved. The next fax shows whether the provider accepts them.

When a check cannot finish, it says so. The other checks still show their results.

The summary at the top counts what does not work and what needs attention. **Copy results** and **Download** give the same sentences as plain text. The text contains no passwords, no keys and no provider replies.

## Other actions

- **Restart Faxbot** asks Faxbot to stop. It works only when **Allow restarting Faxbot from here** is on (**System → Diagnostics**). With Docker Compose, the `api` service starts again by itself, usually in a few seconds. Elsewhere, your process manager must start it.
- **Read saved settings again** asks Faxbot to read its saved settings (`POST /admin/settings/reload`). It never applies changes that wait for a restart.
- The **Database** card shows the type of database, whether Faxbot can reach it and what you can see.
- **Also check the S3 bucket** adds a check that Faxbot can reach the online storage bucket. The settings check below includes it.

## Command line

```
faxbot system diagnostics run      # check now
faxbot system diagnostics show     # show the last results, without a new check
faxbot system diagnostics engine registrations|contacts|calls|faxes
```

## Related endpoints

- `POST /admin/diagnostics/report`: runs every check now. `GET` returns the last results and contacts nothing.
- `GET /admin/diagnostics/engine/{view}`: what the fax engine reports (**System → Developer → Scripts & checks**).
- `POST /admin/diagnostics/run`: every saved setting as a value, for older clients and scripts.
- `GET /health/ready`: the shared readiness check that the Overview uses.
- `GET /admin/db-status`: the **Database** card.
- `POST /admin/restart`: the optional restart request.
