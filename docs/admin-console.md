# Admin Console

The console is the web app Faxbot serves at `/admin/ui/`. Sign in with a username and password, or with an API key. Every screen follows the signed-in person's permissions: a page they may not use is not shown, and the server enforces the same rules.

## Six areas

The left panel lists six areas. Each holds the pages for that part of your installation.

<div class="grid cards" markdown>

- :material-view-dashboard: **Overview**
  What Faxbot is doing, what could improve, everyday fax counts and what needs attention.
  [Open](admin-console/overview.md)

- :material-cash: **Savings & optimization**
  Capabilities, opportunities, facts to establish, savings results, spending, charges, invoices, prices and plans.
  [Open](admin-console/costs.md)

- :material-fax: **Faxes**
  Received, Sent, Send a fax, Expected, Forms and Case packets.
  [Open](admin-console/faxes.md)

- :material-tune: **Delivery setup**
  Numbers and mailboxes, email delivery, providers and accounts, routing rules, and provider-specific settings.
  [Open](admin-console/numbers.md)

- :material-contacts: **Recipients**
  The numbers you fax and your direct-delivery partners.
  [Open](admin-console/recipients.md)

- :material-cog: **Administration**
  People and access, setup, security, documents and retention, system health, audit, logs and Developer pages.
  [Open](admin-console/system.md)

</div>

## Finding your way

- **An address for every page.** Each page has its own address, such as `#/delivery/trunk` or `#/admin/audit`. Back, Forward, reload and shared links open the same page. Older addresses keep working and open the page that now does that job.
- **Where you are.** The line above each page names its area and page; select the area to go back to it.
- **Your menu.** The lower-left menu shows who is signed in and their role. It offers Change password, My sessions, My API keys, Appearance (Light, Dark or Match my system) and Sign out.
- **Providers by name.** The trunk is named by its carrier or phone system ("Telnyx", "Avaya IP Office") everywhere: Overview, Sent, Received, Spending and the command line. When a saved change switches providers or carriers, every page shows the new names at once.

## Saving settings

Settings pages show what is saved and change it with **Apply settings**. Some changes take effect at once; others wait until Faxbot restarts, and the page says so with **Restart now** when restarting from the console is allowed. See [Saving settings](admin-console/settings.md).

Settings only the owner may change are shown, but disabled, to everyone else, with "Only the owner of this installation can change this." Settings set when Faxbot was installed (environment-only settings) are shown read-only where they matter, with "Set when Faxbot started" or "Not set"; a secret's value is never shown.

## When the console does not load

The console is served when the deployment sets `ENABLE_LOCAL_ADMIN=true` and the built console is at `/app/admin_ui/dist` in the container (`api/admin_ui/dist` locally). Those are deployment settings, not console settings. Storage credentials for S3 come from the server's own environment or role; the console never stores or shows them. Where several API processes run, a change that waits for a restart needs every process stopped and started again.

## The command line

The command line has commands for sending and managing faxes, forms and expected faxes, as well as for numbers, recipients, providers, costs, access and the system. See the [command line reference](reference/cli.md).

## Demo (simulated)

A hosted demo with simulated data is at <https://faxbot.net/admin-demo/>. It makes no external calls.
