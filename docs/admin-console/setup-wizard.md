# Setup Wizard

Setup walks through the most important settings step by step. It opens with the installation's current values and never resets a configured installation to defaults.

## 1. Choose providers

Review the default provider, the outbound override and the inbound override. An empty override uses the default provider. For example, default Phaxio with outbound `my-provider` and an empty inbound override sends through `my-provider` and receives through Phaxio.

Installed providers stay selectable. When Setup has no credential panel for a provider installed from a manifest, configure it in **Tools → Plugins**. Receiving is turned on separately, and choosing a provider does not prove that faxes will arrive.

## 2. Configure credentials

Edit the fields of the provider that sends your faxes. Hidden secrets stay as they are unless you type a replacement or clear the field.

- Phaxio: API Key, API Secret, separate Callback Token, Status Callback URL and outbound signature verification. Set Public API URL to a reachable HTTPS endpoint. An empty status URL derives from the public URL. Verification disabled rejects outbound callback updates; original-account polling continues.
- Sinch: Project ID, API Key and API Secret. Its direct-upload path does not need a provider-fetch PDF URL.
- Documo: API Key and sandbox selection.
- HumbleFax: Access Key, Secret Key and an optional From Number. HumbleFax only sends faxes, so it is not offered as an inbound provider.
- SignalWire: Space URL, Project ID, API Token and From number; configure the signing key and other shared fields in Settings.
- SIP/Asterisk: AMI host, port, user/password and station ID. Keep AMI private and configure the separate Asterisk trunk deployment.
- FreeSWITCH: gateway and caller ID. Configure ESL and the result hook separately; Setup does not install a working telephony stack.

Public API URL is a setting, not a tunnel launcher. Start a tunnel yourself, paste its address here or in Settings, then apply. The older `setup-phaxio-tunnel.sh` script only edits the `.env` file used when a new installation first starts; it does not change an existing installation. See [Public Access](../setup/public-access.md).

**Check Supplied Outbound Credentials** only uses explicit, unmasked values for supported Phaxio, Sinch and SIP checks. Sinch's current check tests presence, not authentication. Custom and other unsupported providers show a Diagnostics/Plugins note. These checks do not send a fax or verify inbound document readiness.

**Show Active Callback Details** shows the callback addresses Faxbot is using now, regardless of unsaved edits. **Simulate Inbound Record** adds a test record; it is not a real received fax and does not prove a usable PDF. **Watch New Inbound Log Events** watches the logs until you stop it; a test record also counts as an event.

## 3. Review security settings

Review public HTTPS enforcement, audit logging and how long document download links last. These are individual settings, not a compliance profile. **Require API Key** no longer turns off authentication: every request needs an API key or a signed-in session. Use Settings for other security, storage and receiving fields.

## 4. Apply and export

**Apply Changes** sends only the fields you changed. If nothing changed, nothing is saved. If someone else saved settings after Setup loaded them, Setup keeps your edits and pauses until you select **Reload (discard draft)** to load the current values.

The result says whether the changes are already in use or wait for a restart. For a restart, stop every Faxbot API process and start the installation again, then reload to confirm. Setup does not restart Faxbot or leave the page on its own. **Done** closes Setup; it reads **Done (discard draft)** when you have unsaved edits.

**Export .env Template** downloads the saved settings as a `.env` template with secrets hidden. It is unavailable while you have unsaved edits. Copy and Download are offered, but the template is not a complete backup and does not change anything. See [Settings](settings.md).

## Check a document

1. In Settings, turn on **Disable outbound fax sending** and restart if Faxbot asks for it. Confirm that sending is off before you continue.
2. Open **Send**, attach a test PDF or TXT within the upload limit shown, and select **Queue**.
3. In **Jobs**, check that the fax is held and that its prepared document looks right. Held faxes are never sent, even after sending is turned back on.
4. For a real delivery check, configure the provider, turn sending on and send to a number you control. A fax being accepted is not the same as it being delivered; check the result in Jobs and in your provider account. See the [Phaxio delivery check](../tools/phaxio-e2e-test.md).

## Provider guides

- [Phaxio](../setup/phaxio.md)
- [Sinch](../setup/sinch.md)
- [Documo](../setup/documo.md)
- [HumbleFax](../setup/humblefax.md)
- [SIP/Asterisk](../setup/sip-asterisk.md)
- [FreeSWITCH](../setup/freeswitch.md)
- [SignalWire](../setup/signalwire.md)
