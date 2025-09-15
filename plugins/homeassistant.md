---
layout: default
title: Home Assistant Health Plugin (Sample)
parent: Plugins
nav_order: 10
permalink: /plugins/homeassistant.html
---

# Home Assistant Health Plugin (Sample)

This sample plugin fetches daily health metrics from Home Assistant and reports them via Faxbot. It’s a scaffold you can run today while the full plugin loader lands.

What it does
- Fetches sensors: `sensor.weight`, `sensor.heart_rate`, `sensor.blood_pressure`
- Builds a brief text report
- Sends the report to a fax number via your running Faxbot API

Requirements
- Node 18+
- Faxbot API running locally (`http://localhost:8080`) with an API key
- Home Assistant URL and a long‑lived access token

Setup
1) Configure env
```
export HOME_ASSISTANT_URL=https://ha.local:8123
export HOME_ASSISTANT_TOKEN=<your_ha_token>
export FAXBOT_URL=http://localhost:8080
export FAXBOT_API_KEY=<your_faxbot_key>
export FAX_TO=+15551234567
```
2) Run the sample
```
node scripts/run-plugin-homeassistant.js
```

Notes
- The runner uses Faxbot’s `/fax` endpoint with a generated text file; Faxbot converts text to PDF automatically.
- The sample plugin lives at `plugins/samples/homeassistant/` and exports `initPlugin(faxbotAPI)`; the runner provides a minimal Faxbot adapter with `sendFax` and `sendHealthReport`.
- This is a sample/integration plugin (category: integration); it is not an outbound provider.

Links
- Home Assistant docs: https://www.home-assistant.io/docs/authentication/
