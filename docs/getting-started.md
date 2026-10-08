
# Getting Started

Welcome to Faxbot! This section will help you get up and running quickly.

[:material-wrench: Setup Wizard](admin-console/setup-wizard.md){ .md-button .md-button--primary }
[:material-monitor-dashboard: Admin Console](admin-console.md){ .md-button }
[:material-cloud-lock: Deployment](deployment.md){ .md-button }

<div class="grid cards" markdown>

- :material-rocket-launch: **Launch Faxbot**  
  Start the API and open the Admin Console.  
  [Follow the guide](#launch-faxbot)

- :material-wrench: **Complete the Setup Wizard**  
  Pick a backend, paste creds, apply.  
  [See steps](#complete-the-setup-wizard)

- :material-cog: **What to do next**  
  Provider playbooks, keys, storage, diagnostics.  
  [Next steps](#what-to-do-next)

</div>

## What is Faxbot?

Faxbot is a proprietary, self-hosted fax API. Use, modification, distribution, resale, or hosted access to current Faxbot software requires prior written permission from David Montgomery. See the [license](https://github.com/DMontgomery40/Faxbot/blob/main/LICENSE).

- Simple REST API for sending faxes
- Multiple backend options (cloud and self‑hosted)
- AI assistant integration via MCP
- Installation-local configuration, access controls and audit settings
- Developer SDKs for Node.js and Python

## Launch Faxbot

1. For first bootstrap, copy `.env.example` to `.env`. Once canonical state exists, use Settings for server configuration changes.
2. On a Mac with Colima, create the Colima virtual machine directly on your local network from the start, so that fax over IP (T.38) can work with a carrier trunk (see [Network for fax over IP](setup/network.md)). Colima can't change this later without creating a new virtual machine.

    ```sh
    colima start --network-address --network-mode bridged --network-interface "$(route -n get default | awk '/interface:/{print $2}')" --network-preferred-route
    ```

3. Start the API: `docker compose up -d --build api`
4. Open the Admin Console at `http://localhost:8080/admin/ui/`.

??? tip "Console not found?"
    Set the deployment gate `ENABLE_LOCAL_ADMIN=true`, install the built UI at `/app/admin_ui/dist` in the container or `api/admin_ui/dist` locally and restart the serving API. These UI deployment inputs are separate from canonical runtime settings; `.env` edits/restart do not import canonical provider/security changes.

## Complete the Setup Wizard

1. In the console, open **Setup Wizard**.
2. Review the loaded default provider and independent outbound/inbound overrides. Preserve existing custom selections.
3. Change the intended credentials and security fields; leave unchanged stored masks alone.
4. Apply and inspect active/desired status. Pending changes require every API worker to stop and the installation restart; confirm the desired revision is active afterward. Provider selection and saved settings do not prove delivery.
5. For document checks without transmission, enable [disabled sending](setup/test-mode.md) in Settings and confirm it is active. New uploads remain held rather than becoming simulated successes.

## What to do next

- Follow provider guides under [Backends](setup/index.md) for credentials, networking, and HIPAA notes.
- Manage keys, storage, inbound receiving, and diagnostics from the Admin Console tabs (each screen links to matching docs).
- Integrate your app using the [Node](sdks/node.md) or [Python](sdks/python.md) SDK once outbound faxing is verified.

## Need Help?

See our [Contributing guide](https://github.com/DMontgomery40/Faxbot/blob/main/CONTRIBUTING.md) for support options. Mention which backend you’re using so we can point you to the right playbook.
