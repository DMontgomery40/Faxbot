# Setup Wizard

Setup (**System → Setup**, and **Providers → Add or change a provider**) walks through the most important settings step by step. It opens with the installation's current values and never resets a configured installation to defaults.

## 1. Choose providers

Choose one provider for **Sending** and one for **Receiving**, or **No provider**. The choices are grouped: fax services, your own fax line through a carrier (local carriers first for your country), your phone system (Avaya IP Office, Avaya Aura), and FreeSWITCH under Advanced. Choosing a carrier or phone system sets the trunk and its carrier in one step, and Faxbot names the trunk by it everywhere. This step also sets the installation country and the office's time zone. Receiving offers only providers that can receive faxes (Phaxio, Sinch and the SIP trunk, plus installed plugins that receive). Faxbot needs a sending provider even when it mainly receives, so Setup asks for one before it saves receiving on its own.

**Next** saves the step. Setup never keeps unsaved choices from one step to another: each step saves its own changes when you move on with **Next** or **Back**, and a step that cannot be saved stays open with the reason. Choosing the SIP trunk for the first time, or moving away from it, takes effect after Faxbot restarts. Setup says so in one sentence with a **Restart now** button; Faxbot restarts, comes back within a few seconds, and Setup reloads and says "Faxbot restarted and is using the saved settings." on the same step. When restarting from the console is turned off, the sentence says to run `docker compose restart api` on the server.

Installed provider plugins stay selectable. When Setup has no fields for a provider installed from a manifest, set it up under **System → Developer → Provider plugins**.

## 2. Connect providers

Setup shows one section for each provider in use, headed **For sending: …** and **For receiving: …**. A provider used for both appears once, as **For sending and receiving: …**. Hidden secrets stay as they are unless you type a replacement or clear the field. A key set in `.env` shows **Set in .env** and cannot be changed here.

- Phaxio: API Key, API Secret, separate Callback Token, an optional address for status updates and **Check that status updates come from Phaxio**. Set **This server's public address** to a reachable HTTPS address; an empty status address uses it. Verification disabled rejects outbound callback updates; original-account polling continues.
- Sinch: Project ID, API Key and API Secret. Its direct-upload path does not need a provider-fetch PDF URL.
- Documo: API Key and sandbox selection.
- HumbleFax: Access Key, Secret Key and an optional From Number. HumbleFax only sends faxes, so it is not offered for receiving.
- eFax: App ID, API key and User ID from eFax's welcome email, an optional caller ID and station name and, when eFax receives, how often Faxbot checks eFax, whether it deletes each stored fax from eFax and an optional notification secret. eFax needs no callback address; with the secret, eFax can tell Faxbot the moment a fax arrives. See [eFax](../setup/efax.md).
- SignalWire: Space URL, Project ID, API Token and From number; configure the signing key and other shared fields in Settings.
- Your carrier or phone system: the carrier trunk form (see [SIP trunk](../setup/sip-trunk.md)), shown whenever the trunk sends or receives. **Apply and connect** saves the form, restarts Asterisk with the trunk when no call is up (Docker Compose install) and shows the trunk check on the same step: the transport Faxbot registered over, whether the carrier answers its checks, Faxbot's internet address and "No ports need to be opened or forwarded." When it sends, the **Fax station ID** is the number receiving machines show. When it receives, the step says "Received faxes reach Faxbot: ready." or what keeps them from Faxbot; the secret Asterisk sends with each received fax is created and written by Faxbot, so there is nothing to type. The fax engine connection (address, port, user name and password) sits under **Fax engine connection (advanced)**: Faxbot creates the password the first time it starts with the SIP trunk in use and shares it with Asterisk itself (a password set in `.env` wins), so change these only for a fax engine you run yourself, and keep it on your private network.
- FreeSWITCH: gateway and caller ID. Configure its control connection and the result step on its page; Setup does not install a working telephony stack.

Save or undo changes in the fax line form before you move on; Setup does not leave the step while the form has unsaved changes. The trunk form and Setup share one saved version of the settings, so saving one never makes the other refuse a save.

**This server's public address** appears when a cloud provider is in use. It is a setting, not a tunnel launcher. Start a tunnel yourself, paste its address here or in Settings, then move on. See [Public Access](../setup/public-access.md).

**Check these credentials** (Phaxio and Sinch) only uses explicit, unmasked values. Sinch's check tests presence, not authentication. These checks do not send a fax or verify inbound document readiness.

For a cloud provider that receives, **Show callback details** shows the callback addresses Faxbot is using now. **Add a test received fax** adds a test record to **Faxes → Received**; it is not a real received fax and does not prove a usable PDF. **Wait for a received fax** watches for one for a minute; a test record also counts.

## 3. Security

Review **Require HTTPS for document links**, **Record events** and how long document links for fax services last. These are individual settings, not a compliance profile. **Require API Key** no longer turns off authentication: every request needs an API key or a signed-in session. Use Settings for other security, storage and receiving fields.

## 4. Delivery options

Optional delivery routes, direct delivery and email delivery. You can change them later under Providers → In use, Numbers → Email delivery and Recipients → Partners.

## 5. Finish

Finish shows what sends and what receives faxes. Every step has already saved its changes. If someone else saved settings after Setup loaded them, Setup keeps your edits and pauses until you select **Reload (discard changes)** to load the current values.

**Send a test fax** (optional) sends one test page, made by Faxbot, to the number you enter, once, when you select it; Setup never sends it by itself and never sends it again. The page has a large "Faxbot test page" heading, the date and time with time zone, your organization name if one is set, and "Sent through HumbleFax to +17205550100" (your provider and number), and nothing personal. Setup follows it live ("HumbleFax is sending the test page…", or "The call is in progress…" over the SIP trunk) and ends with one sentence, such as "Sent: the receiving machine confirmed the test page." or "The call connected but no fax data came back from the carrier.", and the carrier's cost for the call once it is known. After a T.38 call with no fax data back, Setup says "Faxbot now uses audio fax for new calls; send another test when you are ready." (or offers **Use audio fax for new calls** if Faxbot could not switch by itself). Your provider may charge for the test fax.

**Receive a test fax** (optional) shows the number to fax, for the SIP trunk "Send a fax to +15555550100 from any fax service; it appears in Received.", and **Wait for a received fax** watches Received for up to five minutes and says when one arrives. The wait checks for new faxes; a test record added before you start waiting does not count. Both tests wait until a pending restart is done.

**Export settings** downloads the saved settings as a `.env` template with secrets hidden. Copy and Download are offered, but the template is not a complete backup and does not change anything. See [Saving settings](settings.md). **Done** closes Setup.

## Check a document

1. Under **Providers → In use**, turn **Sending is on** off and restart if Faxbot asks for it. Confirm that sending is off before you continue.
2. Open **Faxes → Send a fax**, attach a test PDF or TXT within the upload limit shown, and select **Queue**.
3. In **Faxes → Sent**, check that the fax is held and that its prepared document looks right. Held faxes are never sent, even after sending is turned back on.
4. For a real delivery check, configure the provider, turn sending on and send to a number you control. A fax being accepted is not the same as it being delivered; check the result in Sent and in your provider account. See the [Phaxio delivery check](../tools/phaxio-e2e-test.md).

## Provider guides

- [Phaxio](../setup/phaxio.md)
- [Sinch](../setup/sinch.md)
- [Documo](../setup/documo.md)
- [HumbleFax](../setup/humblefax.md)
- [eFax](../setup/efax.md)
- [Your own fax line (SIP trunk)](../setup/sip-asterisk.md)
- [FreeSWITCH](../setup/freeswitch.md)
- [SignalWire](../setup/signalwire.md)
