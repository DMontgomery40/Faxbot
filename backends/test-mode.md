---
layout: default
title: Test Mode (Fax Disabled)
parent: Backends
nav_order: 7
permalink: /backends/test-mode.html
---

# Test Mode (Fax Disabled)

Enable this option when you want to exercise the Admin Console, SDKs, or automated tests without reaching any real fax provider.

## How it works

- Faxbot skips all outbound provider calls and immediately returns simulated responses
- Jobs move through `queued → SUCCESS` with mock metadata (pages, provider SID)
- Upload, PDF conversion, and storage flows still run so you can validate file handling
- Ideal for CI pipelines, demos, and smoke tests in clinics before production go-live

## Configure

1. Admin Console → **Setup Wizard**
2. Choose **Test Mode (Fax Disabled)**
3. Apply. Faxbot writes `FAX_DISABLED=true` in the config store.

You can still enable authentication, storage, and inbound settings—only outbound transmission is short-circuited.

## Tips

- Pair with the [API key helper]({{ site.baseurl }}/admin-console/settings/) to validate auth flows without burning provider credits
- Use the SDKs’ built-in health checks to confirm connectivity while remaining in a sandbox state
- When you are ready for real faxes, rerun the Setup Wizard, choose your provider, and apply the changes. Faxbot rolls back the simulation automatically.
