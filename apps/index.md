---
layout: default
title: Apps
nav_order: 5
has_children: true
permalink: /apps/
---

# Apps

Faxbot includes companion applications that sit on top of the core API.

- Electron (desktop Admin Console shell)
- iOS (Faxbot Companion for iPhone)

These apps never talk directly to providers (Phaxio/Sinch/SIP). They always call the Faxbot REST API and respect the same authentication, rate limits, and file size/type constraints.

