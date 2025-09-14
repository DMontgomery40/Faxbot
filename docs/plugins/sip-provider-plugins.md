---
layout: default
title: SIP Provider Plugins
parent: Plugins
nav_order: 1
permalink: /plugins/sip-provider-plugins.html
---

# SIP Provider Plugins

SIP provider plugins are the most common use case. In Faxbot, plugin type follows capability, not protocol. SIP is the transport layer — your plugin should implement the base class that matches what you’re doing:

- SIP for T.38 fax transmission → `FaxPlugin` (category: `outbound`)
- SIP for SMS via SIP MESSAGE → `MessagingPlugin` (category: `messaging`)
- SIP for voice (if added in the future) → a new `VoicePlugin` type

## Typical SIP Fax Provider

For T.38 fax over SIP, create a `FaxPlugin` with `categories: ["outbound"]` and `capabilities: ["send_fax", "get_status"]`.

Example `manifest.json`:

```json
{
  "id": "faxbot-sip-flowroute",
  "name": "Flowroute SIP Fax",
  "categories": ["outbound"],
  "capabilities": ["send_fax", "get_status"]
}
```

Example plugin skeleton (Python):

```python
from faxbot_plugin_dev import FaxPlugin, SendResult

class FlowrouteSIPPlugin(FaxPlugin):
    async def send_fax(self, to_number, file_path, options=None):
        # 1. Convert PDF to TIFF
        # 2. Connect to Asterisk AMI
        # 3. Originate call with T.38
        # 4. Track via channel events
        return SendResult(job_id="...", backend=self.manifest().id)
```

## Why This Mapping Makes Sense

The existing SIP/Asterisk backend (self‑hosted) is a fax transmission backend alongside Phaxio and Sinch. Each SIP trunk provider (Bandwidth, Flowroute, Twilio SIP, Voxbone, etc.) differs in:

- Authentication methods
- T.38 codec preferences
- Number formatting requirements
- Billing/metadata APIs
- Regional coverage

By packaging each as a plugin, users can install the one that matches their trunk without modifying core.

```bash
pip install faxbot-sip-bandwidth
# or
pip install faxbot-sip-flowroute
```

This design also allows the same SIP provider to offer multiple plugins when appropriate:

- `faxbot-twilio-fax` — `FaxPlugin` using Twilio’s SIP trunking for T.38
- `faxbot-twilio-sms` — `MessagingPlugin` using Twilio’s SMS API

Shared authentication or configuration can live in a common internal module if desired, but keep each plugin focused on its capability to avoid backend leakage in the UI and API.

