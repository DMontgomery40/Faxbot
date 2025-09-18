- Move instructions on branches, repos, which to work in when, to the top of agents.md for agents too lazy to read more than 50 lines, then run that script in parent folder that upddates all agents.md files
  - while the api key management is great, there should be a submenu in api keys, whre it's just users names lists, with a dropdown box, where you can check and uncheck the scopes of their api keys. 
  - OR, if managers shouldn't see user api keys, then set up a user/pass system, instead of a key only login
- ADD FAX COVER PAGE 
  - subject / from / to / date
  - in settings, user configures what fields that want and can add fields, and it will just always auto fill them
  - "Sent from Faxbot.net" at the bottom of every cover page
- FAXBOT.NET mini hero banner in upper left has the old tiny version still  
- SEPERATE INBOUND AND OUTBOUND FAX PROVIDERS
  - i personally want sip inbound with my t38fax.com or whatever sip number, and outbound through sinch
- FIX ALL THE BROKEN LINKS IN ADMIN CONSOLE
- ADD FUNCTIONALITY TO ADMIN CONSOLE OF JUST WRITING TEXT IN A BOX, AND THE API DOES THE WORK OF MAKING IT A TEXT FILE, THIS FUNCTIONALITY IS HOW FAXBOT.APP / IOS APP WORKS
- CLEAN UP UGLY INBOX IN IOS APP, ALLOW "HISTORY" TO BE CLICKABLE SO PEOPLE CAN SEE WHAT THEY SENT
- NO USING ISO DATES ANYWHERE THAT ARE VISABLE TO USER
- ADD OPTION TO SET TIMEZONE IN SETTINGS, DEFAULT TIME SHOULD COME FROM SYSTEM
- FINALLY FIX BROKEN TERMINAL IN ADMIN CONSOLE
- FIX CLOUDFLARE LINK
  - if it doesn't work to start a docker container from the console, which it should, but if it doesn't, just package it into the api itself. it should be on by default with the warning that it's not hipaa compliant, but again we must keep in mind many users don't need hipaa
  - PREP FOR BIGGER CLIENTS BY HAVING BETTER USER MANAGEMENT
  - we have scoped api keys, so it's really just about adding names and stuff, and maybe tracking # of pages per user, so someone at the company doesn't send 20k faxes and nobody knows who did it
    - while the api key management is great, there should be a submenu in api keys, whre it's just users names lists, with a dropdown box, where you can check and uncheck the scopes of their api keys. 
  - OR, if managers shouldn't see user api keys, then set up a user/pass system, instead of a key only login

- **BIG TASK**
MAKE FAXBOT ACTUALLY EASY.  we say it is. it's not. at all. phaxio would take 5 minutes to set up, maybe, but our docs suck on actually doing it and it's an incredly complicated site and has a zillion apis. 
 - big idea:  since sinch has SOOO many api and webhook options, could we use that to our advantage and just write "scripts" for users that set up stuff for them in the backend via curl and command line api calls??? is that possible?? must research if it's possible, or find some kind of way that users can actually get up and going and start faxing in a way that ACTUALLY takes 5 minutes. 
  - next stage should be a subscription option for faxbot for the occasional user who has to send a fax once a month or something, and doesn't want to sign up for all this shit, and doesn't care about having their own number.  they pay us $2/month or $10/year or something, and they can send X amount of faxes, from OUR number, we'll of course tell them what that number is and send them a thing over email saying "here's a copy of the fax you sent".
  - this is huge and it really is the future and the entire point of the consumer side of faxbot, which is why i made it, my question was: why can't i just type out a quick note and fax it from my phone?  now with the faxbot app you can, but, it's all the backend bullshit, and buying phone numbers, and picking providers and going through pricing tiers, and getting a million fuckin secrets and keys and ids ughhhh... i HATE IT. So, we just sign up for an account with whoever, we pay the bill , we send the fax, we take out ALL THE PAIN for users.  again, this is just average individual consumers who aren't hipaa bound, and just want to send an occasional thing. we make the signup wicked easy, all payment options, hopefully they can just click on paypal button, and it will take 2 minutes, and boom they never need to think about the pain of faxing again.   so that would be like faxbot "lite" or something, and then we keep faxbot the super secure hipaa ready platform, as the thing we sell to hospitals and clinics and such. 
  - this brings things to a whole new level of course cause it means aws hosting, and expenses and all sorts of shit.  but it's the right thing to do for the world lol.  hell i'm down to just eat the cost of users sending a few faxes a year without even paying.  what's the section 203 rule on that lol?  we'd probably be responsible for people sending death threats or no?  no as long as they were signing in and it wasn't all anonymous, we don't want to turn this into twitter but over fax.  if there was an app where you could legit just BOOM send a fax, without doing ANYTHING AT ALL, then who are you going to go to if you even need to send faxes at any scale?? us obviously

- DO THIS: 
-    Where to get a macOS .p12 cert

  - Developer ID Application certificate from Apple:
      - Keychain Access → Certificate Assistant → Request a Certificate From a CA… (CSR)
      - Apple Developer → Certificates → Add Developer ID Application → upload CSR → download
      - Import into Keychain, then export as .p12 (with password)
      - Put into GitHub Secrets:
          - MAC_CERT_P12_BASE64 (base64 of the .p12)
          - MAC_CERT_PASSWORD
          - APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, APPLE_TEAM_ID
      - Then run the “Release macOS DMG” workflow (release-macos.yml).


# Missing Features & Mandates — Running List

Updated: 2025-09-17

Purpose: While auditing AGENTS.md against the codebase and syncing the Admin Console and vendor demo, track missing features, drifts, and UX mandates here. Always add new items at the top. Close items with a short note and file/commit reference.

- Admin Console nav parity and cleanup
  - Remove Plugin Builder from UI navigation entirely; keep manifest runtime only. Verify no routes point to `api/admin_ui/src/components/PluginBuilder.tsx`.
  - Group navigation as: Dashboard; Settings → Setup, Settings, Keys, MCP; Tools → Terminal, Diagnostics, Logs, Plugins (feature‑gated), Scripts & Tests.
  - Ensure mobile responsiveness and legibility across common breakpoints for all Tools sub‑tabs (Terminal, Diagnostics, Logs, Plugins, Scripts & Tests).

- Scripts & Tests (backend‑aware)
  - Add Sinch helper card(s) (credentials check, base URL hint) and SignalWire + FreeSWITCH preview helpers alongside existing Phaxio/SIP. File: `api/admin_ui/src/components/ScriptsTests.tsx`.
  - Enforce backend isolation: only show cards and guidance for the active backend; hide inbound helpers unless `INBOUND_ENABLED=true`.
  - Prevent concurrent runs across cards (global lock) and reflect per‑card disable state clearly.
  - Replace any placeholder/cute labels; no “GUI‑first” banners; keep copy plain with tooltip + “Learn more”.
  - Add backend‑aware remediation links in Jobs detail for failed jobs (Phaxio/Sinch/SIP/FS specific). File: `api/admin_ui/src/components/JobsList.tsx`.

- Terminal (local‑only)
  - Confirm Tools → Terminal is present and functional; requires admin key and `ENABLE_LOCAL_ADMIN=true`.
  - Improve Safari input handling by consolidating key events (migrate any lingering onKeyPress to onKeyDown). File: `api/admin_ui/src/components/Terminal.tsx`.
  - Seed initial prompt/output (e.g., welcome + `pwd && ls`) and auto‑fit at mount. Also replicate fake FS seeding in vendor demo.
  - Verify server WS endpoint gating/requirements and helpful error when unavailable. Files: `api/app/main.py:/admin/terminal`, `api/app/terminal.py`.
  - Replace login input onKeyPress in `api/admin_ui/src/App.tsx` with onKeyDown for cross‑browser consistency.

- Diagnostics
  - Extend `/admin/diagnostics/run` with SignalWire and FreeSWITCH checks (preview) to match UI hints. File: `api/app/main.py`.
  - Ensure “Open Settings” button routes to the grouped Settings screen (fixed; re‑verify).
  - Add provider‑specific troubleshooting links; keep backend isolation.

- Plugins (v3)
  - Show Tools → Plugins only when `FEATURE_V3_PLUGINS=true`; ensure no Plugin Builder routes or mentions remain in UI.
  - Validate curated registry parity with server `/plugin-registry`; ensure demo mocks mirror current curated entries.
  - Security passes: manifests respect allowed domains, timeouts, HTTPS in HIPAA; redact secrets in UI.
  - Add redaction policy in manifest runtime to scrub sensitive fields from any UI‑surfaced debug output.

- API Admin Actions & Terminal tests
  - Add tests for `GET /admin/actions` and `POST /admin/actions/run` (gating via `ENABLE_ADMIN_EXEC`, allowlist, backend gating, timeout). New file: `api/tests/test_admin_actions.py`.
  - Add tests for `/admin/terminal` WebSocket auth paths (env key vs DB key with `keys:manage`), local‑only enforcement, requirement failure messaging. New file: `api/tests/test_terminal_ws.py`.

- Inbound receiving UX & endpoints
  - UI: Toggle + storage/KMS guidance, token TTL controls, list/detail with secure download; provider callback guidance (`/phaxio-inbound`, `/sinch-inbound`, SIP internal). File: `api/admin_ui/src/components/Inbound.tsx`.
  - API: Re‑verify `/_internal/asterisk/inbound`, `/phaxio-inbound`, `/sinch-inbound` logic, token TTL defaults, retention cleanup. File: `api/app/main.py`.

- Docs (Jekyll site parity)
  - Add “Scripts & Tests” page documenting all scripts under `scripts/` and `node_mcp/scripts/` with usage; keep in parity with main.
  - Add “Terminal” page (Admin Console local terminal: security, gating, usage). Link from Tools → Terminal help.
  - Remove any references to Plugin Builder UI; prefer manifest runtime notes.
  - Ensure all UI help links use `DOCS_BASE_URL` and are backend‑specific.

- Release hygiene (main branch)
  - Strip internal planning artifacts from main/origin: `AGENTS.md`, `USER_TODO.md`, `v3_plans/`, `@v3_console_plans/`, any planning notes and TODOs; keep only public docs (Jekyll site) in main. Provide a release script/Make target that assembles a clean tree.
  - Verify no internal planning docs ship in Docker images.

- Vendor admin demo (faxbot.net)
  - Ensure nav parity and Tools sub‑tabs mirror Console: includes Terminal (demo), Diagnostics, Logs, Plugins (if feature‑gated), Scripts & Tests (backend‑aware sample).
  - Seed demo Terminal with a fake filesystem and initial `ls` output. Validate Plugins demo list matches curated registry.

- Security & logging
  - Confirm no secrets or PHI are logged in UI/network; UI shows IDs/metadata only. Ensure Action outputs are truncated and redacted.
  - Confirm strict HTTPS and HMAC verification defaults for cloud backends in HIPAA mode.

- SDKs & OpenAPI
  - Verify SDKs are at version 1.1.0 and aligned with current `/openapi.json`. Ensure Admin UI types match spec; avoid drift.

- Full AGENTS.md audit
  - Produce a line‑by‑line checklist mapping each statement to concrete code/files and note deltas or TODOs. Save at `docs/AGENTS_AUDIT.md`; keep updated.

— End of current list —
