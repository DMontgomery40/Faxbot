# Diagnostics

Open **System → Diagnostics** and select **Run Diagnostics** to inspect the active installation. Above the checks, the **Database** card says what kind of database Faxbot uses, whether it can reach it, what you can see, and warns about a database file outside the data folder (`GET /admin/db-status`). The summary identifies the active outbound provider, active inbound provider, default provider, active and desired revisions, and configuration generation. A pending revision does not change the configuration being checked.

## What the results mean

- **Pass / Fail** describe applicable checks. Local readiness requires the active outbound configuration, required native connections and storage, database access, Ghostscript, and writable document directories.
- **Warning** identifies a configuration concern, such as disabled audit logging or public HTTPS enforcement.
- **Info** describes feature state or metadata. Disabled receiving, remote plugin installation, and an unneeded AMI connection are not automatically failures.
- **Not applicable** means a check is not required for the active configuration.

Readiness checks inspect the captured active outbound adapter. They do not authenticate every remote provider account, verify callback reachability, or prove delivery. Inbound profile presence also does not establish that an external fax can be received. Use a controlled end-to-end fax to establish those results.

Native Asterisk diagnostics include AMI connection state, a non-default AMI password, and the inbound secret when receiving is enabled. A custom HTTP manifest using the same provider name is assessed as its captured adapter rather than assumed to be native Asterisk.

System checks create and remove their own temporary files. Storage reports its configured type and whether the active inbound provider requires it. The optional S3 bucket access probe runs only when inbound storage is required, a bucket is configured, and **Also check the S3 bucket** is on (each Diagnostics switch has its own **Apply**).

Installed plugin and trait metadata are shown as structured values. This inventory describes installed files; active readiness uses the captured revision. Long values are shortened on screen and remain available in the JSON export.

## Actions

- **Open Settings** opens Providers → In use to review desired settings, apply explicit changes using the loaded revision, and inspect active versus pending state. Editing environment files does not update an initialized canonical store.
- **Open Send** to choose a document and a destination you control. Held mode creates durable work without dispatch. Real mode can submit to the active provider when sending is enabled. Follow the resulting job and verify the received document to establish delivery.
- **Copy results** copies the diagnostic result. **Download results** requests a browser download; verify that the browser saved it.
- **Restart Faxbot** asks Faxbot to stop, only when the installation allows it; the reply confirms the request, not a completed restart. With Docker Compose the `api` service starts again by itself (`restart: unless-stopped`), usually within a few seconds; elsewhere your process manager must start it again. Where several API processes run, a pending settings change waits until every one of them has stopped and started again.

See the [Diagnostics Matrix](diagnostics-matrix.md) for follow-up actions, [Saving settings](settings.md) for configuration, and [Deployment](../deployment.md) for host preparation.

## Related endpoints

- `POST /admin/diagnostics/run`: this diagnostic result.
- `GET /health/ready`: shared local readiness checks.
- `GET /admin/health-status`: dashboard health and durable job counts.
- `GET /admin/db-status`: the database card.
- `POST /admin/restart`: optional process restart request.
