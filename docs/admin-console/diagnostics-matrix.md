# Diagnostics Matrix

This page tells you what to do about each result on [Diagnostics](diagnostics.md). The button next to a result opens the page that this table names.

| Result | What to do |
| --- | --- |
| No sending provider is set up | Go to **System → Setup**. It walks you through choosing a provider. |
| The provider did not accept Faxbot's sign-in details | Go to **Providers → In use**. Enter the keys again. Copy them from the provider's own dashboard. |
| Faxbot could not reach the provider | Check the server's internet connection. Then select **Check now**. A provider outage clears by itself. |
| Faxes may or may not have arrived | Go to **Faxes → Sent** and confirm each fax. Faxbot never sends them again by itself. |
| The last fax failed | Go to **Faxes → Sent**. Open the fax to see what the receiving machine or the provider said. |
| Faxbot stopped trying to fetch received faxes | Go to **Faxes → Received**. Open each fax and select **Fetch again**. |
| The email server did not accept the user name or password | Go to **Numbers → Email delivery**. For Gmail, use an app password. |
| Faxbot could not email some faxes | Go to **Faxes → Received**. Retry each fax when email works again. |
| The fax engine does not run, or it refuses Faxbot's sign-in | Go to **Providers → carrier trunk**. Check that Asterisk runs and that the manager password matches. |
| The carrier did not accept Faxbot's sign-in details | Go to **Providers → carrier trunk**. Check the username and password from the carrier's dashboard. Then select **Apply and connect**. |
| Received faxes do not reach Faxbot | Go to **Providers → carrier trunk** and select **Apply and connect**. This writes what the fax engine needs. |
| Fax over IP (T.38) is off | The sentence tells you why. **Providers → carrier trunk** has the details. Audio fax keeps working meanwhile. |
| Faxing over the internet: your network needs one change | Go to **Providers → carrier trunk → Network for fax over IP**. It shows the fix for your platform. Audio fax keeps working meanwhile. See [Network for fax over IP](../setup/network.md). |
| Little disk space is left | Go to **System → Storage & retention**. Keep faxes for a shorter time, or give the server more space. Faxbot stops receiving when the disk is full. |
| Faxbot cannot write fax files | On the server, check the disk and the permissions of the data folder. |
| The document converter is missing | Install Ghostscript (`gs`) where Faxbot runs. The Docker image includes it. |
| No time zone is chosen | Go to **System → Setup → Time zone**. |
| Some saved changes take effect after Faxbot restarts | Select **Restart Faxbot** on Diagnostics, or restart the service. |
| The audit log, request limits or secure links are off | Go to **System → Audit log** or **System → Security**. |
