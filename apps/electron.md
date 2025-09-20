---
layout: default
title: Electron (Desktop)
parent: Apps
nav_order: 2
permalink: /apps/electron/
---

# Electron (Desktop Admin Console)

The Electron shell packages the Admin Console as a desktop app. It embeds the same React/MUI Admin UI used in the browser and talks only to the Faxbot REST API.

## Availability
- Installers are provided by Faxbot for macOS, Windows, and Linux. For access, contact support.

## Status
- Electron runs the UI in a sandboxed renderer and uses a preload script for limited IPC.

## Setup
- Use your server’s base URL (default http://localhost:8080) or a secure tunnel when not running locally.
- Feature parity with the web Admin Console is required; no desktop‑only features.
