
# Deployment

Services
- `api`: FastAPI service and console (required)
- `asterisk`: SIP/Asterisk backend (only when SIP is selected for inbound or outbound)
- `faxbot-mcp` and `faxbot-mcp-py-sse`: MCP servers (optional, `--profile mcp`). They get no Faxbot key: each MCP client sends its own.

Ports
- `8080`: API
- `3001`: MCP Streamable HTTP (Node)
- `3004`: MCP Streamable HTTP (Python)
- `3003`: MCP SSE (Python, for older clients)
- SIP/Asterisk: nothing is published; `5038` (AMI) stays on the Compose network. Only a public host with IP sign-in adds `docker-compose.public.yml` (SIP and a 32-port media range).

Storage and database
- `FAX_DATA_DIR` for PDFs/TIFFs (default `./faxdata`)
- SQLite for dev; use Postgres in production (`DATABASE_URL`)
- S3/S3‑compatible for inbound artifacts; for SSE‑KMS see AWS docs below

Configuration and activation

- Environment variables and the legacy plugin JSON file are read once, when a new installation starts for the first time, except credentials: provider keys, passwords and secrets in `.env` are read at every start and are the values in force (see [Credentials from .env](admin-console/settings.md#credentials-from-env)). Keep container ports, mounts and telephony service settings in the deployment configuration. With Docker Compose, `.env` is optional.
- On an existing installation, change settings on the admin console's Settings screen and apply.
- When Settings asks for a restart, stop every API process and start the installation again, then load Settings to confirm nothing is pending. Restarting one process while others keep running, or using the reload button, is not enough.
- The Compose file restarts the `api` and `asterisk` services after any exit (`restart: unless-stopped`) until you stop them with `docker compose stop` or `down`.
- Back up the database, the installation key file and stored documents together; the exported `.env` template is not a backup. See [Back up and restore](#back-up-and-restore).

Public URL and TLS

- In Settings, set `PUBLIC_API_URL` to your HTTPS endpoint
- `ENFORCE_PUBLIC_HTTPS=true` for production with cloud backends
- For quick testing, use a tunnel:
  - Cloudflare Tunnel: <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/>
  - ngrok (HTTP): <https://ngrok.com/docs/guides/http/>

Artifacts cleanup
- `ARTIFACT_TTL_DAYS` to delete old PDFs/TIFFs after completion
- `CLEANUP_INTERVAL_MINUTES` controls sweep frequency

Security
- Set a strong `API_KEY` before the first start. It becomes the installation key, which creates the first owner; use named users and scoped keys afterwards
- Never expose AMI (5038) publicly
- Use HTTPS for callbacks and public endpoints

## Upgrade an installation

A database upgrade needs the **whole installation** stopped: every API process and container that uses the database, including workers on other hosts that share a PostgreSQL database. `faxbot admin` refuses to run when a server answers on the configured address or holds the data folder's lock files, but it cannot see processes on other hosts.

With Docker Compose:

```bash
docker compose stop api                     # and anything else using this installation
docker compose build api                    # or pull the new image
docker compose run --rm --no-deps api faxbot admin status
docker compose run --rm --no-deps api faxbot admin migrate
docker compose up -d api
docker compose exec api faxbot health
```

Back up before `migrate`:

- An installation made by this release or later: use [`faxbot admin backup`](#back-up-and-restore).
- An installation from before October 2026 has no installation key file yet, so `faxbot admin backup` refuses it. Copy the database and the data folder yourself. With SQLite in the default volume, both are in `/faxdata`:

    ```bash
    docker compose run --rm --no-deps -v "$PWD/backups:/backups" api tar -C /faxdata -cf /backups/faxdata-before-upgrade.tar .
    ```

    With PostgreSQL, also take a `pg_dump` of the database.

`faxbot admin migrate` upgrades the database in place. Stored faxes, documents, mailboxes, number routes and API keys stay as they were. An upgraded installation that never set `FAX_BACKEND` keeps using Phaxio; a new installation starts with no provider until you choose one. Starting the new version would also upgrade the database, but running `migrate` first shows the result before anything serves requests.

After upgrading an installation from before October 2026:

1. Sign in to the console with the installation key and [create the first owner](security/access-control.md#create-the-first-owner).
2. Run `faxbot keys list`. Keys that had every permission show **Needs review** and do not work until approved: give the key's integration a role (`faxbot access grant "<integration>" "Fax operator"`), then approve the key with the permissions it needs (`faxbot keys approve <key id> --for "<integration>" -p fax:read`).
3. Older keys with specific permissions keep working, but they no longer open received documents. Create a new key for each app that needs documents.
4. Make a fresh backup with `faxbot admin backup`.

See the [release notes](release-notes.md) for everything that changed.

## Back up and restore

`faxbot admin backup` copies the database, the data folder, the installation key and the direct delivery key, with a checksum manifest. The installation key is what lets a restored installation read its saved settings and provider credentials. Details are in [Command line: backup and restore](operations/cli.md#backup-and-restore).

Back up with Docker Compose. Use a folder outside the data volume:

```bash
docker compose stop api
docker compose run --rm --no-deps -v "$PWD/backups:/backups" api faxbot admin backup /backups/2026-10-03
docker compose start api
```

The backup files belong to the container's user and are readable only by it. Keep the folder as safe as the installation: it holds private keys and fax documents.

Restore on a new host or into a new, empty data volume:

```bash
docker compose run --rm --no-deps -v "$PWD/backups:/backups" api faxbot admin restore /backups/2026-10-03
docker compose up -d api
```

The restored installation keeps its own installation key, users, keys, received faxes and settings, including provider credentials. `API_KEY` in `.env` does not replace the restored installation key; credentials set in `.env` are applied at start, as on every start. People sign in with their usual passwords. Restore refuses a backup whose files do not match its manifest, and it does not overwrite an existing installation unless you add `--force`. Restore to the same database and data folder locations the installation used; with the default Compose file that is `/faxdata`.

References
- AWS S3 SSE‑KMS: <https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html>
- [Faxbot reference guides](reference/index.md)
