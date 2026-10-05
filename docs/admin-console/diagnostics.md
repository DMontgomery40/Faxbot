# Diagnostics

**System → Diagnostics** answers one question: is Faxbot working, and if not, what should you do about it? Each check is a single sentence, and when one needs you, a button next to it opens the page where you fix it.

The page shows the last results as soon as it opens. The first time you open it after Faxbot starts, it runs the checks on its own; after that, select **Check now** whenever you want fresh results. The checks only read: they never send a fax, change a setting or cost money.

## What is checked

**Sending.** Faxbot confirms that your sending provider still accepts its sign-in details, using HumbleFax's read-only account lookup or eFax's sign-in. It lists any faxes that may or may not have arrived (confirm those in **Sent**) and summarizes the last delivered fax and the past seven days.

**Receiving.** If a different provider receives your faxes, Faxbot checks that provider's sign-in too. It flags received faxes it stopped trying to fetch and shows when the last fax arrived. For each email delivery, Faxbot signs in to the email server and leaves again without sending anything, and it tells you if any received faxes could not be emailed.

**Fax engine.** Faxbot checks that its own fax engine is running and how many calls are in progress. With a carrier trunk it also checks that the carrier accepts Faxbot's sign-in and answers its checks, that received faxes reach Faxbot, whether your network allows fax over IP (T.38) ("Faxing over the internet"), why T.38 is off when Faxbot turned it off, and how the last call went.

**This server.** Faxbot checks its database, the free disk space where faxes are stored (it warns below 2 GB or 5%), that it can write fax files, and that its document converter is installed. It also shows the time zone used for times, any saved settings waiting for a restart, and online storage when you use it.

**Security.** Faxbot reports whether the audit log, protection from overload and secure links for fax services are on.

Phaxio, Sinch, SignalWire and Documo have no read-only sign-in check, so for them Faxbot can only confirm that the sign-in details are saved; the next fax shows whether the provider accepts them. If a check can't finish, it says so, and the other checks still show their results.

The summary at the top counts what isn't working and what needs attention. **Copy results** and **Download** give you the same sentences as plain text, with no passwords, keys or provider replies in them.

## Other actions

- **Restart Faxbot** asks Faxbot to stop. It only works when **Allow restarting Faxbot from here** is on. With Docker Compose the `api` service starts again by itself within a few seconds; elsewhere, your process manager has to start it.
- **Read saved settings again** asks Faxbot to reload its saved settings (`POST /admin/settings/reload`). Changes that are waiting for a restart still wait.
- The **Database** card shows what kind of database Faxbot uses, whether it can reach it, and what you can see in it.
- **Also check the S3 bucket** adds a check that Faxbot can reach your online storage bucket.

## Command line

```
faxbot system diagnostics run      # check now
faxbot system diagnostics show     # show the last results without checking again
faxbot system diagnostics engine registrations|contacts|calls|faxes
```

## Related endpoints

- `POST /admin/diagnostics/report` runs every check now; `GET` returns the last results without contacting anything.
- `GET /admin/diagnostics/engine/{view}` shows what the fax engine reports (**System → Developer → Scripts & checks**).
- `POST /admin/diagnostics/run` returns every saved setting as a value, for older clients and scripts.
- `GET /health/ready` is the shared readiness check the Overview uses.
- `GET /admin/db-status` feeds the **Database** card.
- `POST /admin/restart` is the optional restart request.
