# Admin Console

The console is the web app Faxbot serves at `/admin/ui/`. Sign in with a username and password, or with an API key. Every screen follows the signed-in person's permissions: a page they may not use is not shown, and the server enforces the same rules.

## Eight areas

The left panel lists eight areas. Each holds a few pages.

<div class="grid cards" markdown>

- :material-view-dashboard: **Overview**
  What needs a person now, and what sending costs.
  [Open](admin-console/overview.md)

- :material-fax: **Faxes**
  Received, Sent and Send a fax.
  [Open](admin-console/faxes.md)

- :material-dialpad: **Numbers**
  Your numbers, mailboxes, email delivery and sender identity.
  [Open](admin-console/numbers.md)

- :material-contacts: **Recipients**
  The numbers you fax, partners and case packets.
  [Open](admin-console/recipients.md)

- :material-cloud: **Providers**
  What sends and receives, and each provider in use.
  [Open](admin-console/providers.md)

- :material-cash: **Costs**
  Spending, prices and plans, savings and recommendations.
  [Open](admin-console/costs.md)

- :material-lock-open: **Access**
  Users, groups, roles, keys and phones, and sessions.
  [Open](admin-console/access.md)

- :material-cog: **System**
  Setup, security, storage, audit log, diagnostics, logs and Developer.
  [Open](admin-console/system.md)

</div>

## Finding your way

- **An address for every page.** Each page has its own address, such as `#/providers/trunk` or `#/system/audit`. Back, Forward, reload and shared links open the same page. Older addresses keep working and open the page that now does that job.
- **Where you are.** The line above each page names its area and page; select the area to go back to it.
- **Your menu.** The lower-left menu shows who is signed in and their role. It offers Change password, My sessions, My API keys, Appearance (Light, Dark or Match my system) and Sign out.
- **Providers by name.** The trunk is named by its carrier or phone system ("Telnyx", "Avaya IP Office") everywhere: Overview, Sent, Received, Spending and the command line. When a saved change switches providers or carriers, every page shows the new names at once.

## Saving settings

Settings pages show what is saved and change it with **Apply settings**. Some changes take effect at once; others wait until Faxbot restarts, and the page says so with **Restart now** when restarting from the console is allowed. See [Saving settings](admin-console/settings.md).

Settings only the owner may change are shown, but disabled, to everyone else, with "Only the owner of this installation can change this." Settings set when Faxbot was installed (environment-only settings) are shown read-only where they matter, with "Set when Faxbot started" or "Not set"; a secret's value is never shown.

## The command line

`faxbot`, the command line, follows the same eight areas: `faxbot received`, `sent`, `numbers`, `recipients`, `providers`, `costs`, `access` and `system`. See the [command line reference](reference/cli.md).

## Demo (simulated)

A hosted demo with simulated data is at <https://faxbot.net/admin-demo/>. It makes no external calls.
