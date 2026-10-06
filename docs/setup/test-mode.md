# Fax Disabled: Held Test Jobs

Turn **Sending is on** off to check document preparation and **Faxes → Sent** without sending anything.

## Configure an existing installation

1. Open **Providers → In use** and turn **Sending is on** off (it asks first).
2. Click **Apply settings**. If Faxbot asks for a restart, stop every API process and start the installation again.
3. Reload the page and confirm that sending is off and no restart is pending.
4. In **Faxes → Send a fax**, attach a synthetic PDF or TXT document and select **Queue test fax**. The server refuses a stale queue-only form if sending has been enabled; reload the page before proceeding.

`FAX_DISABLED=true` in the environment only applies when a new installation starts for the first time. Changing it later does not change an existing installation; use **Providers → In use** instead.

## What happens

- PDF/TXT/TIFF preparation preserves supported source content and produces real document artifacts; disabled sending does not select placeholders.
- Acceptance creates a held job with `dispatch_mode=held` and `delivery_state=held`. It has no issued provider attempt, provider acknowledgement or simulated success.
- Held jobs never transmit automatically when sending is re-enabled. A fabricated result callback cannot mark them delivered.
- Disabling sending pauses ready work. It cannot recall an attempt already issued; an existing attempt may still produce a result.
- Inbound handling is configured separately. The disabled flag is not a general block on every possible diagnostic network check.

## Enable real sending

Open Settings, turn **Disable outbound fax sending** off, apply, and restart if Faxbot asks for it. Confirm the active state before creating a new request for a controlled destination. Previously held jobs remain held. Review uncertain or historical jobs against the original provider before taking action; never blindly resubmit them.

See [API Tests](../tools/api-tests.md) for isolated internal checks and [Settings](../admin-console/settings.md) for saving, restarts and recovery.
