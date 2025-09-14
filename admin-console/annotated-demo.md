---
layout: default
title: Annotated Demo (Interactive)
parent: Admin Console
nav_order: 6
permalink: /admin-console/annotated-demo/
---

# Annotated Admin Demo (Interactive)

This is a live, embedded view of the Admin Demo with overlay callouts. Use the Toggle Annotations button to see highlights for a typical “Compose and Send” flow.

<style>
.annot-wrap { position: relative; width: 100%; max-width: 1100px; margin: 0 auto; }
.annot-frame { width: 100%; height: 720px; border: 1px solid #222; border-radius: 8px; }
.annot-layer { position: absolute; inset: 0; pointer-events: none; }
.annot { position: absolute; border: 2px solid #e11; background: rgba(255,0,0,0.08); color: #fff; font-size: 12px; padding: 6px 8px; border-radius: 6px; box-shadow: 0 0 0 2px rgba(0,0,0,0.2) inset; }
.annot .label { display: block; font-weight: 600; margin-bottom: 2px; }
.annot .hint { display: block; color: #ffd; font-weight: 400; opacity: 0.9; }
.annot.hidden { display: none; }
.annot-toggle { margin: 12px 0 16px; }
/* Approximate positions relative to the demo’s default layout */
.callout-nav-compose { left: 16px; top: 140px; width: 180px; height: 36px; }
.callout-to-field { left: 340px; top: 220px; width: 360px; height: 46px; }
.callout-send-btn { left: 350px; top: 360px; width: 170px; height: 40px; }
</style>

<button class="annot-toggle" onclick="document.querySelectorAll('.annot').forEach(n=>n.classList.toggle('hidden'))">Toggle Annotations</button>

<div class="annot-wrap">
  <iframe class="annot-frame" src="https://faxbot.net/admin-demo/" title="Admin Demo" loading="lazy"></iframe>
  <div class="annot-layer">
    <div class="annot callout-nav-compose">
      <span class="label">1) Open Compose</span>
      <span class="hint">Click the Compose button in the left navigation</span>
    </div>
    <div class="annot callout-to-field">
      <span class="label">2) Recipient</span>
      <span class="hint">Enter phone number in E.164 format (e.g., +15551234567)</span>
    </div>
    <div class="annot callout-send-btn">
      <span class="label">3) Send (Simulated)</span>
      <span class="hint">Click to queue a demo fax and observe the Outbox</span>
    </div>
  </div>
</div>

Notes
- This demo is simulated — no files are transmitted. For a real send, use your local Admin Console after backend configuration.
- Annotations are approximate (responsive layout may shift elements). Toggle them off if they obscure your view.

