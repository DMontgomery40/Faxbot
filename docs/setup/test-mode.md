# Fax Disabled: Held Test Jobs

Use **Disable fax sending (queue only)** to check upload preparation and operator workflows without issuing new fax attempts.

## Configure an existing installation

1. Open **Settings**, load the desired revision and turn on **Disable fax sending (queue only)**.
2. Apply the changed field. If it is pending, stop every API worker and restart the installation.
3. Load Settings again and confirm the desired revision is active and sending is disabled.
4. In **Send**, attach a synthetic document and use **Queue**. The server refuses a stale queue-only form if another operator has enabled sending; refresh Send before proceeding.

For an installation without canonical state, `FAX_DISABLED=true` is a bootstrap environment value. Changing that environment variable on an existing installation does not replace its canonical revision.

## What happens

- PDF/TXT/TIFF preparation preserves supported source content and produces real document artifacts; disabled sending does not select placeholders.
- Acceptance creates a held job with `dispatch_mode=held` and `delivery_state=held`. It has no issued provider attempt, provider acknowledgement or simulated success.
- Held jobs never transmit automatically when sending is re-enabled. A fabricated result callback cannot mark them delivered.
- Disabling sending pauses ready work. It cannot recall an attempt already issued; an existing attempt may still produce a result.
- Inbound handling is configured separately. The disabled flag is not a general block on every possible diagnostic network check.

## Enable real sending

Load Settings, turn the disabled control off, apply and complete any required coordinated restart. Confirm the active state before creating a new request for a controlled destination. Previously held jobs remain held. Review uncertain or historical jobs against the original provider before taking action; never blindly resubmit them.

See [API Tests](../tools/api-tests.md) for isolated internal checks and [Settings](../admin-console/settings.md) for revisions and recovery.
