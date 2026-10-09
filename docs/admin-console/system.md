# System

## Setup

**System → Setup** (`#/system/setup`) is the [Setup wizard](setup-wizard.md): providers, their accounts, security, delivery options and test faxes. Its first step also sets the installation country and the office's time zone, so the times in fax emails match your clocks.

## AI analysis

**System → AI analysis** (`#/system/analysis`) sets whether Faxbot runs scheduled analyses, which AI service and model to use, and how often to run them. Add the service's API key here. The page also shows the latest result and its evidence. Analysis provides suggestions only; it does not change settings or send faxes.

## Security

**System → Security** (`#/system/security`): how people sign in and how this server is reached. **Require HTTPS for document links**, **Restore from the recovery copy on a fresh start** and **This server's public address**, then, read-only, **Sign-in without HTTPS on a private network**, **Console addresses allowed to sign in** and **Console served by this installation**.

## Storage & retention

**System → Storage & retention** (`#/system/storage`): **Where faxes are kept** (on this server or in S3 storage, with **Check the bucket**), limits and cleanup (largest document, requests per minute for each key, how long document links and sent fax files last), **Export settings** and **Save a recovery copy**, and, read-only, where the installation key and the direct-delivery key are kept.

## Audit log

**System → Audit log** (`#/system/audit`) lists who did what in Faxbot, newest first: **When**, **Who**, **Signed in with**, **Action** ("Changed settings", "Asked for access to the server terminal"), **Changed** ("Mailbox: Billing", "Terminal access code") and **Result** (Done or Refused). Filter by person and by action; **Show older entries** reads further back. Entries are never changed or removed.

Below the list, **Event recording** sets what Faxbot records for Logs: **Record events**, how each event is written, a file on the server, and the system log. Only the owner can change these; they take effect after Faxbot restarts.

## Diagnostics

**System → Diagnostics** (`#/system/diagnostics`) shows the **Database** (its kind, whether Faxbot can reach it, what you can see, and a warning for a file outside the data folder) and runs the [diagnostic checks](diagnostics.md). **Also check the S3 bucket** and **Allow restarting Faxbot from here** each have their own **Apply**, so refusing one never undoes the other. The server's time zone is shown read-only.

## Logs

**System → Logs** (`#/system/logs`) shows recorded events with the columns Time, Event, Fax, Key, Provider, Result, Error, To and From. To search one column, type its name, a colon and the words, such as `provider:sinch` or `result:failed`. Select an entry to see all of it.
