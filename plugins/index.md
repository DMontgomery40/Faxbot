---
layout: default
title: Plugins
nav_order: 5
has_children: true
permalink: /plugins/
---

# Plugins

Faxbot v3 supports modular provider plugins that bind to capability slots at runtime. The plugin type follows the capability, not the transport or protocol.

- Outbound fax → `FaxPlugin` (category: `outbound`)
- Inbound fax → delegated handlers in core + plugin helpers (category: `inbound`)
- Messaging → `MessagingPlugin` (category: `messaging`)
- Auth → `AuthPlugin` (category: `auth`)
- Storage → `StoragePlugin` (category: `storage`)

Key rule: protocols like SIP are transports, not capabilities. Choose the plugin base class based on what the plugin does.

