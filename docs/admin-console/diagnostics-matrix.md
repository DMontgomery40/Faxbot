# Diagnostics Matrix

What to do about each result on [Diagnostics](diagnostics.md). The button next to a result on that page opens the page named here.

| Result | What to do |
| --- | --- |
| No sending provider is set up | **Administration → Setup** walks you through choosing one. |
| The provider did not accept Faxbot's sign-in details | In **Delivery setup → Providers & accounts**, enter the keys again, copied from the provider's own dashboard. |
| Faxbot could not reach the provider | Check the server's internet connection, then select **Check now**. A provider outage clears up on its own. |
| Faxes may or may not have arrived | Confirm each one in **Faxes → Sent**. Faxbot never sends them again by itself. |
| The last fax failed | Open it in **Faxes → Sent** to see what the receiving machine or the provider said. |
| Faxbot stopped trying to fetch received faxes | Open each one in **Faxes → Received** and select **Fetch again**. |
| The email server did not accept the user name or password | Fix it in **Delivery setup → Staff email delivery**. With Gmail, use an app password. |
| Faxbot could not email some faxes | Once email works again, retry each one from **Faxes → Received**. |
| The fax engine isn't running, or refuses Faxbot's sign-in | On **Delivery setup → carrier trunk**, check that Asterisk is running and that the manager password matches. |
| The carrier did not accept Faxbot's sign-in details | On **Delivery setup → carrier trunk**, check the username and password against the carrier's dashboard, then select **Apply and connect**. |
| Received faxes don't reach Faxbot | Select **Apply and connect** on **Delivery setup → carrier trunk**; it writes what the fax engine needs. |
| Fax over IP (T.38) is off | The sentence explains why, and **Delivery setup → carrier trunk** has the details. Audio fax keeps working in the meantime. |
| Faxing over the internet: your network needs one change | **Delivery setup → carrier trunk → Network for fax over IP** shows the fix for your setup; see also [Network for fax over IP](../setup/network.md). Audio fax keeps working in the meantime. |
| Little disk space is left | In **Administration → Documents & retention**, keep faxes for a shorter time, or give the server more space. Faxbot stops receiving when the disk is full. |
| Faxbot can't write fax files | Check the disk and the data folder's permissions on the server. |
| The document converter is missing | Install Ghostscript (`gs`) where Faxbot runs. The Docker image already includes it. |
| No time zone is chosen | Choose one in **Administration → Setup → Time zone**. |
| Some saved changes take effect after Faxbot restarts | Select **Restart Faxbot** on Diagnostics, or restart the service. |
| The audit log, protection from overload, or secure links are off | Turn them on in **Administration → Audit log** or **Administration → Security**. |
