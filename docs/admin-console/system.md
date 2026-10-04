# System

## Setup

**System → Setup** (`#/system/setup`) is the [Setup wizard](setup-wizard.md): providers, their accounts, security, delivery options and test faxes. Its first step also sets the installation country and the office's time zone, so the times in fax emails match your clocks.

## Security

**System → Security** (`#/system/security`): how people sign in and how this server is reached. **Require HTTPS for document links**, **Restore from the recovery copy on a fresh start** and **This server's public address**, then, read-only, **Sign-in without HTTPS on a private network**, **Console addresses allowed to sign in** and **Console served by this installation**.

## Storage & retention

**System → Storage & retention** (`#/system/storage`): **Where faxes are kept** (on this server or in Amazon S3, with **Check the bucket**), limits and cleanup (largest document, requests per minute for each key, how long document links and sent fax files last), **Export settings** and **Save a recovery copy**, and, read-only, where the installation key and the direct-delivery key are kept.

## Remote access

**System → Remote access** (`#/system/remote`): the tunnel phones use to reach Faxbot from outside your network.

## Audit log

**System → Audit log** (`#/system/audit`) lists who did what in Faxbot, newest first: **When**, **Who**, **Signed in with**, **Action** ("Changed settings", "Opened the terminal"), **Changed** ("Mailbox: Billing", "Terminal access code") and **Result** (Done or Refused). Filter by person and by action; **Show older entries** reads further back. Entries are never changed or removed.

Below the list, **Event recording** sets what Faxbot records for Logs: **Record events**, how each event is written, a file on the server, and the system log. Only the owner can change these; they take effect after Faxbot restarts.

## Diagnostics

**System → Diagnostics** (`#/system/diagnostics`) shows the **Database** (its kind, whether Faxbot can reach it, what you can see, and a warning for a file outside the data folder) and runs the [diagnostic checks](diagnostics.md). **Also check the S3 bucket** and **Allow restarting Faxbot from here** each have their own **Apply**, so refusing one never undoes the other. The server's time zone is shown read-only.

## Logs

**System → Logs** (`#/system/logs`) shows recorded events with the columns Time, Event, Fax, Key, Provider, Result, Error, To and From. To search one column, type its name, a colon and the words, such as `provider:sinch` or `result:failed`. Select an entry to see all of it.

## Developer

The Developer pages use developer words; the rest of the console does not.

- **API & SDKs**: security status, held test faxes, and the quickstart with the SDKs' current version; **Documentation address** and the files Faxbot reads.
- **AI assistants**: the MCP servers and their settings, and the assistant servers' own settings, read-only with their variable names.
- **Terminal**: a shell on the server for people allowed to use it; whether it is on is set when Faxbot is installed.
- **Scripts & checks**: test faxes and a simulated received fax.
- **Provider plugins**: **Use provider plugins**, always listed so plugins can be turned on here, and the installed plugins once they are on.
