# Setup Wizard

Setup is a guided editor of canonical desired settings. On entry it loads the installation's existing values and revision; it does not reset a configured installation to provider or security defaults.

## 1. Choose providers

Review the default provider, outbound override and inbound override. Empty overrides independently use the default provider. For example, default Phaxio plus outbound `my-provider` and an empty inbound override means desired outbound `my-provider`, desired inbound Phaxio.

Installed provider IDs remain selectable. A custom manifest provider keeps its selection; configure its credentials in **Tools → Plugins** when Setup has no panel for it. Enable inbound handling separately. A provider selection is not proof of inbound receipt.

## 2. Configure credentials

Edit the desired outbound provider's fields. Unchanged stored masks preserve secrets; replacement values or explicit empty fields change them.

- Phaxio: API Key, API Secret, separate Callback Token, Status Callback URL and outbound signature verification. Set Public API URL to a reachable HTTPS endpoint. An empty status URL derives from the public URL. Verification disabled rejects outbound callback updates; original-account polling continues.
- Sinch: Project ID, API Key and API Secret. Its direct-upload path does not need a provider-fetch PDF URL.
- Documo: API Key and sandbox selection.
- HumbleFax: Access Key, Secret Key and an optional From Number. HumbleFax only sends faxes, so it is not offered as an inbound provider.
- SignalWire: Space URL, Project ID, API Token and From number; configure the signing key and other shared fields in Settings.
- SIP/Asterisk: AMI host, port, user/password and station ID. Keep AMI private and configure the separate Asterisk trunk deployment.
- FreeSWITCH: gateway and caller ID. Configure ESL and the result hook separately; Setup does not install a working telephony stack.

Public API URL is shared configuration, not a tunnel launcher. For an existing installation, start a tunnel manually, paste its URL here or in Settings, then apply. The legacy `setup-phaxio-tunnel.sh` edits bootstrap `.env` and restarts Compose; it does not patch an initialized canonical store. See [Public Access](../setup/public-access.md).

**Check Supplied Outbound Credentials** only uses explicit, unmasked values for supported Phaxio, Sinch and SIP checks. Sinch's current check tests presence, not authentication. Custom and other unsupported providers show a Diagnostics/Plugins note. These checks do not send a fax or verify inbound document readiness.

**Show Active Callback Details** reads the active inbound projection independently of your desired draft. **Simulate Inbound Record** creates a synthetic record; it is not provider delivery or proof of a usable PDF. **Watch New Inbound Log Events** observes logs only and can be stopped; a synthetic event can satisfy that observation.

## 3. Review security settings

Review API-key enforcement, public HTTPS enforcement, audit logging and PDF token TTL. These controls are individual configuration choices, not a compliance profile or delivery certification. Use Settings for other security, storage and inbound fields.

## 4. Apply and export

**Apply Changes** submits only fields differing from the loaded baseline, with the loaded desired revision. Unchanged Apply makes no mutation. Conflicts retain your draft and pause editing/saving until an explicit **Reload (discard draft)** loads the current revision.

Read the result: settings are durably saved and either active or pending restart. For pending changes, every API worker must stop and the installation restart; confirm the active/desired identity afterward. Setup does not restart the server or automatically navigate away. **Done** is explicit; it labels unsaved draft discard.

**Export Redacted Desired Template** calls the server export and is blocked while your draft is unsaved. Its output is the server's current desired template, with secret values masked. Copy and Download are available, but the template is not a complete recovery backup or a mechanism to apply changes. See [Settings](settings.md).

## Check a document

1. In Settings, enable **Disable fax sending (queue only)** and complete any required coordinated restart. Confirm it is active before submitting.
2. Open **Send**, attach a synthetic PDF/TXT within the displayed active upload limit and use **Queue**.
3. In **Jobs**, inspect its held state and prepared document. Held jobs have no issued attempt and never become automatic deliveries when sending is re-enabled.
4. For a real delivery check, use a controlled destination after configuring the provider and confirming sending is active. Acceptance alone is not delivery; inspect the issued attempt and the original provider's result. See the [Phaxio delivery check](../tools/phaxio-e2e-test.md).

## Provider guides

- [Phaxio](../setup/phaxio.md)
- [Sinch](../setup/sinch.md)
- [Documo](../setup/documo.md)
- [HumbleFax](../setup/humblefax.md)
- [SIP/Asterisk](../setup/sip-asterisk.md)
- [FreeSWITCH](../setup/freeswitch.md)
- [SignalWire](../setup/signalwire.md)
