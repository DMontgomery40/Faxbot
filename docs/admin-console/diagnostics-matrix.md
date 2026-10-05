# Diagnostics Matrix

What to do about each result on [Diagnostics](diagnostics.md). The button next to a result opens the page named here.

| Result | What to do |
| --- | --- |
| No sending provider is set up | **System → Setup** walks through choosing one. |
| The provider did not accept Faxbot's sign-in details | **Providers → In use**: enter the keys again, copied from the provider's own dashboard. |
| Faxbot could not reach the provider | Check the server's internet connection, then **Check now**. A provider outage clears by itself. |
| Faxes may or may not have arrived | **Faxes → Sent**: confirm each one. Faxbot never sends them again on its own. |
| The last fax failed | **Faxes → Sent**: open it to see what the receiving machine or provider said. |
| Faxbot stopped trying to fetch received faxes | **Faxes → Received**: open each one and select **Fetch again**. |
| The email server did not accept the user name or password | **Numbers → Email delivery**: for Gmail, use an app password. |
| Faxes could not be emailed | **Faxes → Received**: retry each one once email works. |
| The fax engine is not running or refuses Faxbot's sign-in | **Providers → carrier trunk**: check that Asterisk runs and the manager password matches. |
| The carrier did not accept Faxbot's sign-in | **Providers → carrier trunk**: check the user name and password from the carrier's dashboard, then **Apply and connect**. |
| Received faxes do not reach Faxbot | **Providers → carrier trunk** → **Apply and connect** writes what the fax engine needs. |
| Fax over IP (T.38) is off | The sentence says why; **Providers → carrier trunk** has the details. Audio fax keeps working meanwhile. |
| Little disk space is left | **System → Storage & retention**: shorten how long faxes are kept, or give the server more space. Faxbot stops receiving when the disk is full. |
| Faxbot cannot write fax files | Check the data folder's disk and permissions on the server. |
| The document converter is missing | Install Ghostscript (`gs`) where Faxbot runs; the Docker image includes it. |
| No time zone is chosen | **System → Setup** → Time zone. |
| Some saved changes take effect after Faxbot restarts | **Restart Faxbot** on Diagnostics, or restart the service. |
| The audit log, request limits or secure links are off | **System → Audit log** or **System → Security**. |
