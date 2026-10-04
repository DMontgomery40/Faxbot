# Access Control

Faxbot decides what each person, app and device may do. You manage this in the admin console under **Settings**, on the **Users**, **Groups**, **Roles**, **Access**, **Keys** and **Sessions** screens. The console only shows the screens you are allowed to use, and the server checks every request again.

How people and apps sign in is covered in [Authentication](authentication.md).

## Users and integrations

**Users** are people. They sign in with a username and password. Someone with the `users:manage` permission adds them on the **Users** screen and receives a temporary password to pass on. At first sign-in the new user must choose a password of at least 12 characters. An administrator can reset a password, which issues a new temporary one, or disable a user, which ends their sessions and stops their keys.

**Integrations** are apps, scanners, scripts and other systems. They have no password and only use [API keys](#keys). Add them on the same **Users** screen. Each iPhone that pairs with Faxbot becomes its own integration named after the device.

**The installation key** is the `API_KEY` value saved in the installation's configuration. It can do everything. Use it to create the first owner and to recover owner access, not for daily work. If `API_KEY` is empty, the installation key cannot sign in at all.

## Groups

A group collects users and integrations so you can give a role once instead of person by person. Everyone in the group gets the group's access. A disabled group gives nobody anything. Manage groups on the **Groups** screen; changing them needs `groups:manage`.

## Roles

A role is a named set of permissions. Faxbot includes six built-in roles that cannot be changed:

| Role | What it is for | What it allows |
| --- | --- | --- |
| **Owner** | Full control of the installation | Everything, including the host terminal, restarts, host actions and owner recovery. |
| **Administrator** | Running the installation day to day | Everything except the host terminal, restarts, host actions and owner recovery. |
| **Fax Operator** | Sending and handling faxes | Send faxes, see sent faxes and their documents, refresh their status, see received faxes and their documents, and own, assign and complete work on them. |
| **Fax Viewer** | Reading faxes only | See sent faxes and their documents, see received faxes and their documents, and see the work queue. |
| **Auditor** | Reviewing activity | Read the security audit, see sent and received fax details and the work queue without opening documents, and export work evidence without the original document. |
| **Host Operator** | Looking after the server | Use the host terminal, restart Faxbot, run approved host actions, and read diagnostics and settings. |

On the **Roles** screen, people with `roles:manage` can also create custom roles from any mix of permissions.

### Permissions

| Area | Permission | Allows |
| --- | --- | --- |
| Sent faxes | `fax:send` | Send faxes |
| | `fax:read` | See sent faxes and their delivery details |
| | `fax:document` | Open the document of a sent fax |
| | `fax:refresh` | Ask the provider for a sent fax's latest status |
| | `fax:reconcile` | Attach the provider's fax ID to a fax whose result is uncertain |
| Received faxes | `inbound:list` | List received faxes |
| | `inbound:read` | See the details of a received fax |
| | `inbound:document` | Open the document of a received fax |
| People and access | `users:read`, `users:manage` | See, or add and change, users and integrations |
| | `groups:read`, `groups:manage` | See, or change, groups and their members |
| | `roles:read`, `roles:manage` | See, or create and change, roles |
| | `grants:read`, `grants:manage` | See, or change, who has which role where |
| | `keys:manage` | Create, change, replace and revoke API keys |
| | `sessions:read`, `sessions:revoke` | See, or end, other people's sessions |
| | `owner:recover` | Create owners and make owner-only changes |
| Mailboxes | `mailboxes:read`, `mailboxes:manage` | See, or change, mailboxes and fax number routing |
| Work | `work:read` | See the work queue for received documents, their history and counts |
| | `work:manage` | Assign, reassign, mark done and reopen work |
| | `work:export` | Export an item's evidence; the original document also needs `inbound:document` |
| | `work:import` | Import documents from another system; an integration key can carry it |
| Configuration | `settings:read`, `settings:write` | See, or change, installation settings |
| | `providers:read`, `providers:write` | See, or change, fax provider settings |
| | `providers:install` | Install provider plugins |
| | `tunnels:read`, `tunnels:manage` | See, or change, the remote access tunnel |
| | `tunnels:pair` | Create iPhone pairing codes |
| Monitoring | `diagnostics:read` | Run diagnostics |
| | `logs:read` | Read the activity log on the **Logs** screen |
| | `audit:read` | Read the security audit |
| Host | `host:restart` | Restart Faxbot |
| | `host:actions` | Run approved maintenance actions on the server |
| | `host:terminal` | Use the host terminal |

Some settings, such as the installation key, the public address, signature checks and file locations, can only be changed by an Owner, even when someone has `settings:write`.

## Who has access, and where

A role does nothing until you give it to someone. On the **Access** screen, **Give access** asks for three things:

- **Who**: a user, an integration or a group.
- **Role**: a built-in or custom role.
- **Where**: everything in the installation, one mailbox, or one person's own faxes.

Access given on everything covers every fax and mailbox. Access on a mailbox covers only the faxes received into it. Access on a person's own faxes covers the faxes that person sends.

Work permissions follow the received document: `work:read`, `work:manage` and `work:export` on a mailbox cover the work for faxes received into it. Assigning work never gives access; an owner must already see the document. See [Work](../operations/work.md).

Each person sends faxes from their own space, so anyone who should send needs a role with `fax:send` either on everything or on their own faxes.

You can only give out permissions you hold yourself. Changes to an Owner's account, or to the installation key, need a full Owner.

## Mailboxes and fax number routing

Received faxes land in a mailbox. On the **Access** screen:

- **Mailboxes** lists your mailboxes. Add one per team or purpose, for example "Front desk" or "Billing".
- **Fax numbers** decides which mailbox receives faxes sent to each of your numbers. Faxbot compares digits only, so `+1 (303) 555-0199` and `13035550199` match.

When a fax arrives, Faxbot uses the oldest routing rule whose number matches and whose mailbox is enabled. A fax that matches no rule stays unassigned. Only people with access on everything can see unassigned faxes.

To let a team see only their own faxes, give their group **Fax Viewer** or **Fax Operator** on their mailbox. Changing mailboxes and routing needs `mailboxes:manage`.

## Keys

API keys let apps, scanners and phones use Faxbot. Manage them on the **Keys** screen, which needs `keys:manage`.

- **Every key belongs to a user or an integration.** It can never do more than its owner, even if the owner later loses access.
- **Every key has its own permission list.** When you create a key, choose only what that app needs, for example `fax:send` and `fax:read` for a scanner. The key can do something only when both its owner and its permission list allow it.
- **Expiry.** A key can have an expiry date. Sessions started with the key end when it expires.
- **Replace key** issues a new secret and stops the old one at once. **Revoke** stops the key for good.
- **The full key is shown once.** Copy it into the app straight away; Faxbot cannot show it again.

Keys created before this access system appear as **Needs review** when they had unrestricted access. They do not work until someone approves them: choose who the key belongs to and what it may do. Older keys with specific permissions keep sending and reading fax details as before, but they cannot open fax documents. Create a new key for an app that needs documents.

### Keys for the iPhone app

When the iPhone app pairs, Faxbot creates an integration for that device and gives it a key. The key can send faxes, see sent faxes and their documents, and see received faxes and their documents. It is listed on the **Keys** screen under the device's name, where you can revoke it if the phone is lost. See [iOS App](../apps/ios.md#pair-the-app).

## Create the first owner

A new installation has no users. To create the first owner:

1. Set `API_KEY` in the environment before the installation starts for the first time. Faxbot saves it then; later `.env` edits do not change it.
2. Open the admin console. While no owner exists, the sign-in page says so and shows the **Installation key** field first; paste the `API_KEY` value. (Once an owner exists, the page asks for a username and password, and **Sign in with API key** is the way to use a key.)
3. Select **Create the first owner**, then enter a username and display name. When no fax provider is set up yet, closing the temporary password opens Settings → Setup.
4. Copy the temporary password. It is shown only once.
5. Sign out, then sign in with the new username and temporary password. Faxbot asks you to choose a new password.

The **Create the first owner** prompt appears only while no owner exists.

## Recover owner access

If every owner is locked out, sign in to the console with the installation key (`API_KEY`), as in the steps above. The installation key can do everything an Owner can, including resetting an owner's password on the **Users** screen or adding a new owner.

Keep `API_KEY` somewhere safe, such as a password manager. On an existing installation, only an Owner can change it, through the settings API (`PUT /admin/settings` with `api_key`); the Settings screen does not show it, and editing `.env` does not change it.

If `API_KEY` is empty or lost, the host operator can set a new one with Faxbot stopped: run `faxbot admin recover-owner` on the server, which saves a fresh installation key, records it in the security audit and shows it once. Then start Faxbot and create an owner with `faxbot owner enroll` using that key. See [Recover owner access](../operations/cli.md#recover-owner-access) in the command line guide. There is no way to do this over the network.

## Audit

Faxbot records access changes and sign-ins in a security audit: users, integrations, groups, roles, access, keys, mailboxes and routing rules that were added or changed, and every session started, whether allowed or refused. Received faxes also record which mailbox they were placed in.

Read the security audit through `GET /access/audit`, which needs `audit:read`. The console does not show it yet. The **Logs** screen, which needs `logs:read`, shows the separate activity log, including terminal use and iPhone pairing.

## API reference

Everything on these screens is also available through the `/access` API. See [Access and Sign-in API](../reference/access-api.md).
