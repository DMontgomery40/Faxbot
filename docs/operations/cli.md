# Command line

The `faxbot` command does what the Admin Console does, from a terminal or a script: send faxes, read received ones, manage users and keys, change settings, check delivery costs, pair phones. The console's built-in terminal is the one exception; you are already in one. It talks to a running Faxbot server with an API key, the same way the SDKs do.

Five `faxbot system` commands work on a **stopped** installation instead, straight from its database and files: `status`, `migrate`, `recover-owner`, `backup` and `restore`.

Every command and option is listed in the [command line reference](../reference/cli.md).

## How commands are grouped

`faxbot send` and `faxbot status` send a fax and follow it. Everything else sits under the same eight areas as the console:

| Command | What it covers |
| --- | --- |
| `faxbot received` | Received faxes, their owners and their email delivery |
| `faxbot sent` | Sent faxes, their documents and their delivery |
| `faxbot numbers` | Your fax numbers, mailboxes and email delivery settings |
| `faxbot recipients` | The numbers you fax: routes, sending together, partners and case packets |
| `faxbot providers` | Fax providers, their settings and your phone line (`faxbot providers trunk`) |
| `faxbot costs` | Spending, carrier charges, prices and plans |
| `faxbot access` | People, groups, roles, keys, sign-ins and paired phones |
| `faxbot system` | Settings, checks, logs, the security log, profiles, backups and restarts |

Command names from before these eight areas, such as `faxbot jobs list`, no longer work. The [release notes](../release-notes.md#renamed-commands) list the new name for each.

## Install

The command line ships with the API in `api/app/cli`.

- **Docker:** the image includes it. Run `docker compose exec api faxbot system health`.
- **From a checkout:** install the API requirements, then run it from the `api` folder:

    ```bash
    pip install -r api/requirements.txt -r python_mcp/requirements.txt
    cd api
    python -m app.cli --help
    ```

    `make cli ARGS="system health"` does the same from the repository root.

- **As a `faxbot` command:** `pip install -e api` installs the `faxbot` script from the checkout. Install the requirements first.

Shell completion: `faxbot --install-completion`.

## Connect to your server

Every remote command needs the server address and an API key:

```bash
export FAXBOT_URL=https://fax.example.com
export FAXBOT_API_KEY=fbk_live_...
faxbot access me
```

`faxbot access me` shows who the key belongs to and what it may do. Create a key for yourself in the console, or with `faxbot access keys create`.

### Profiles

A profile saves the address and key, so you do not need the variables:

```bash
faxbot --url https://fax.example.com system profiles save clinic
```

You are asked for the API key without it being shown. Profiles live in `~/.config/faxbot/config.toml`, which only you can read (mode 600). Saving a profile makes it the default unless you add `--no-use`; choose another with `--profile NAME`, `FAXBOT_PROFILE`, or `faxbot system profiles use NAME`. `faxbot system profiles list` lists profiles without showing keys.

The order is: `--url` and `--key`, then `FAXBOT_URL` and `FAXBOT_API_KEY`, then the profile, then `http://localhost:8080`. A saved key is only ever sent to the address saved with it: when `--url` or `FAXBOT_URL` names a different server, give that server's key with `--key` or `FAXBOT_API_KEY`.

## Everyday use

```bash
faxbot send +15551234567 referral.pdf          # send a fax
faxbot status <fax id>                         # where it is now
faxbot sent list                               # sent faxes, newest first
faxbot received list                           # received faxes
faxbot received pdf <received fax id> -o fax.pdf
faxbot received recover                        # bring in faxes the phone line received but did not hand over
faxbot providers trunk apply                   # connect the saved phone line and show its check
faxbot access users add jsmith --name "Jane Smith"    # shows a temporary password once
faxbot access grants add jsmith "Fax operator"
faxbot access integrations add "Front desk scanner"   # an app or device with its own key
faxbot access grants add "Front desk scanner" "Fax operator"
faxbot access keys create --for "Front desk scanner" -p fax:send -p fax:read   # shows the key once
faxbot costs spending
```

To compare setup options such as joining a partner's network under a budget, write a JSON scenario file and run `faxbot costs portfolio --file scenario.json` (use `--file -` to read standard input). The file is at most 64 KiB. It holds a currency, a period (`horizon`), whose amounts count (`perspective`), a `budget`, up to ten setup items (`nodes`), shared costs (`groups`) and the benefit of each pair of items (`relationships`). Write amounts as quoted decimal text, or `null` when you do not know a value. If a needed amount is `null`, the command lists what to enter instead of a plan. Otherwise it prints an expected plan and a cautious plan. The result comes only from the amounts you enter. It is not a measured saving. The command saves nothing, enrolls no partner and changes no route. The [command line reference](../reference/cli.md#faxbot-savings-portfolio) lists every field.

Users, groups, roles, mailboxes and keys are named the way people know them: a sign-in name, a group name, a mailbox label. Add `--ids` to a list to see internal ids, and use an id when two names are the same.

Secrets that Faxbot shows only once (new API keys, temporary passwords, the device key from pairing and the recovered installation key) are printed once and never saved. Settings and provider credentials are always shown masked. Enter secrets with the hidden prompts (`faxbot system settings set --secret NAME`, `faxbot providers configure phaxio --secret api_key`) rather than on the command line, where they would stay in your shell history.

Each change to users and access sends the access version Faxbot reported a moment before. If someone else changes access at the same moment, the command stops with "Access settings changed while this command was running. Run it again."

## Scripts

Add `--json` to get the API's own answer as JSON. Times in JSON are UTC, as the API sends them; people see local times.

```bash
id=$(faxbot --json send +15551234567 report.pdf | jq -r .id)
faxbot --json status "$id" | jq -r .status
```

Use `--idempotency-key` on `faxbot send` when a script might run twice: sending again with the same key returns the first fax instead of sending a second one. The command line never retries a request on its own.

`--quiet` prints nothing on success, except a secret shown only once, which it prints alone on one line:

```bash
token=$(faxbot --quiet access keys rotate 1a2b3c4d5e6f)
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Done. |
| 1 | The command could not do what you asked; the message says why. |
| 2 | The command was typed wrong (unknown option or missing argument). |
| 3 | No API key, or Faxbot did not accept it. |
| 4 | The key is not allowed to do that. |
| 5 | Not found, or the key cannot see it. |
| 6 | It conflicts with the current state, for example something changed meanwhile or a folder is not empty. |
| 7 | Too many requests; wait and try again. |
| 8 | Faxbot could not be reached or could not finish right now. |
| 9 | Faxbot did not accept the values given. |
| 10 | Faxbot is running, so a command for a stopped installation refused to start. |

With `--json`, a failure prints `{"error": {"message": ..., "exit_code": ..., "status": ..., "detail": ...}}`.

## Local administration

`faxbot system status`, `migrate`, `recover-owner`, `backup` and `restore` open the installation directly. They read the same settings as the server: `DATABASE_URL`, `FAX_DATA_DIR`, `FAXBOT_INSTALLATION_KEY_PATH` and `FAXBOT_DIRECT_KEY_PATH` (or `--database-url`, `--data-dir`, `--key-file` and `--direct-key-file` after the command). Run them where the server runs, with the same environment.

They refuse to run while Faxbot is running: when a server answers at the configured address, or when the installation's lock files show a running server. Stop Faxbot first. With Docker:

```bash
docker compose stop api
docker compose run --rm --no-deps api faxbot system status
docker compose start api
```

### Status and upgrade

- `faxbot system status` shows whether the database is up to date, whether settings changes are waiting for a restart, whether an installation key is set, and how many users, owners, keys, sessions and faxes there are. It never shows secrets.
- `faxbot system migrate` upgrades the database to this version of Faxbot through the same locked upgrade the server runs at start.

### Recover owner access

Use this when nobody can sign in as an owner and the installation key (`API_KEY`) is empty or lost. If you still have the installation key, you do not need it: sign in with the key as described in [Access control](../security/access-control.md#recover-owner-access).

1. Stop Faxbot.
2. Run `faxbot system recover-owner`. It saves a fresh installation key in the installation's settings, with an entry in the security audit, and shows the key **once**. Store it in a password manager. Anything that used the old installation key stops working.
3. Start Faxbot.
4. Create an owner with the new key:

    ```bash
    FAXBOT_API_KEY=<the new key> faxbot access owner enroll --login jsmith --name "Jane Smith"
    ```

5. Sign in to the console with that name and the temporary password it shows, and choose a new password.

Editing `.env` does not change the installation key of an existing installation; this command does.

### Backup and restore

```bash
faxbot system backup /backups/faxbot-2026-10-03
```

With Docker, mount a folder from the host for the backup, outside the data volume:

```bash
docker compose stop api
docker compose run --rm --no-deps -v "$PWD/backups:/backups" api faxbot system backup /backups/2026-10-03
docker compose start api
```

A backup folder holds:

- the database: a copy of the SQLite file, or a consistent export of every PostgreSQL table;
- the data folder (fax documents), without lock files and without a SQLite database that lives inside it, which is copied separately;
- the installation key, without which saved settings and provider credentials cannot be read;
- the direct delivery key, when the installation has one;
- `manifest.json`, with a SHA-256 checksum of every file.

The backup contains private keys and fax documents. The folder is created readable only by you; keep it as safe as the installation itself.

To restore, stop Faxbot, point the environment at the installation's usual locations and run:

```bash
faxbot system restore /backups/faxbot-2026-10-03
```

Restore checks every file against the manifest first, and refuses a backup with changed, missing or added files. It does not replace an existing database, data folder or key unless you add `--force`. Restore to the same database and data folder locations the installation used: Faxbot refuses to start when its saved locations do not match. A PostgreSQL backup must be restored with the same Faxbot version that made it; a SQLite backup from an older version can be restored and then upgraded with `faxbot system migrate`.
