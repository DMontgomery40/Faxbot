High‑level roadmap

  - Phase 1: 5‑Minute Quickstart
  - Phase 2: Provider Auto‑Config “Scripts”
  - Phase 3: Hosted “Faxbot Lite” (multi‑tenant, our numbers)
  - Phase 4: Payments, Quotas, Anti‑Abuse
  - Phase 5: Admin Console Onboarding UX
  - Phase 6: Docs overhaul (task‑oriented)
  - Phase 7: Ops & Telemetry
  - Phase 8: Legal & Policy

  Phase 1 — 5‑Minute Quickstart

  - Phaxio Fast Path (lowest friction)
      - One screen in Setup Wizard with 3 visible fields: API key, secret, Public URL.
      - Inline “Copy webhook” and “Generate HMAC secret” with “Verify callback signature” button (live check).
      - One click “Export working .env” and “Run test fax to yourself” (from Tools).
  - Sinch Fast Path (clear & minimal)
      - Fields: Project ID, API key, secret, Base URL (optional).
      - “Register status webhook” button and “Verify ping” tool.
  - Test Mode (no provider)
      - 30‑second path to send a simulated fax and see “works” quickly. Clear banner “Simulated only.”

  Phase 2 — Provider Auto‑Config “Scripts”
  Goal: Let users click a button and we configure the provider on their behalf (where APIs permit).

  - Approach
      - Add “Provider Setup (Auto)” cards in Tools → Scripts & Tests:
          - “Register callbacks”, “Disable storage” (Phaxio), “Set webhook signing” (Phaxio/Sinch).
      - Under the hood: server runs curated curl/HTTP calls to provider APIs with user‑supplied creds (never
  stored longer than necessary).
  - Deliverables
      - scripts/providers/phaxio/setup.sh and scripts/providers/sinch/setup.sh
      - Matching server endpoints (admin‑only) to run them safely via allowlist executor (we already have “Admin
  actions” pattern).
      - UI buttons to run and show results; display exactly what was changed for transparency.
  - Research items
      - Phaxio: confirm API endpoints for setting callbacks + storage flags; HMAC settings.
      - Sinch Fax v3: confirm webhook registration endpoint; auth model; reference base URLs/regions; rate
  limits.

  Phase 3 — Hosted “Faxbot Lite”
  Goal: 2‑minute consumer path; no provider signup; send from our pool numbers.

  - Product outline
      - Multi‑tenant mode in API (account_id on jobs, tenants on keys).
      - Outbound backend uses our provider accounts (shared pool; generic From).
      - Email receipts (attached PDF), minimal retention by default (e.g., 7 days).
      - Rate limiting per account.
  - Architecture
      - API (FastAPI) behind ALB, S3 for artifacts, KMS for encryption (SSE‑KMS), RDS (Postgres) for tenancy,
  Redis for rate limits.
      - Queue workers (Celery/RQ) for send/convert; ECS or EKS; autoscale.
      - Stripe for billing webhooks (subscription + usage meter).
  - Pricing
      - $2/mo or $10/yr includes X pages/month; overages pay‑as‑you‑go; annual “just works” option.
  - Security/abuse
      - Email verification + optional SMS OTP before first send.
      - Hard caps (pages/day, size limits).
      - Abuse detection (provider error codes, content heuristics, too many recipients, high fail rate).
      - Clear AUP/ToS; “report misuse” flow.

  Phase 4 — Payments, Quotas, Anti‑Abuse

  - Payments
      - Stripe + PayPal (Stripe Checkout supports Apple Pay/Google Pay).
      - Webhooks: provision account, assign default plan, show quota in UI.
  - Quotas
      - Track pages sent this cycle; show “pages left”.
      - Block on hard cap; suggest plan upgrade.
  - Anti‑abuse
      - Maintain blocklists (global + per account).
      - Provider rate‑limits mirrored at ingress.
      - Retain minimal audit metadata (job id, time, to, pages, hashed sender ip/email).

  Phase 5 — Admin Console Onboarding UX

  - A real Onboarding Checklists panel (top of Settings):
      - Pick mode: Lite (hosted) vs Self‑hosted.
      - Provider chosen? Keys valid? Webhooks verified? Test fax sent?
      - 100% progress bar; green checks; “Fix” links jump to the right field.
  - One‑click “Provider Auto‑Config” buttons wired to Phase 2 scripts.
  - “Test Fax” composer inline (PDF or quick text → server renders PDF).

  Phase 6 — Docs overhaul (task‑oriented)

  - Create “Start Faxing in 5 Minutes” page with three tracks:
      - Hosted Lite path
      - Phaxio path
      - Sinch path
  - Task pages, not encyclopedias:
      - “Send your first fax” (literal copy/paste), “Verify webhooks”, “Inbound: get your first inbound PDF”.
  - Tight link mapping from UI (docsBase + anchors already supported in our UI components).

  Phase 7 — Ops & Telemetry

  - Observability
      - Per‑tenant success rate, retries, common failure reasons.
      - Dashboard cards: “Last hour sends”, “Common errors”, “Help link” surfaces (link to docs).
  - Alerting
  - Self‑check
      - Diagnostics screen expands to “Run all checks” with one-click remediation suggestions.

  Phase 8 — Legal & Policy (high level)

  - Section 230: We’re a conduit for user content; consult counsel, but keep audit logs and clear ToS/AUP.
  - DMCA isn’t our path here; but process abuse complaints quickly.
  - Light KYC for Lite to prevent anonymous misuse (email + optional phone OTP).
  - HIPAA stance: Lite is explicitly non‑HIPAA; Enterprise tracks HIPAA defaults (retention, HTTPS required,
  audit).

  Concrete near‑term tasks (I can start now)

  - Provider Auto‑Config prototype (Phaxio)
      - scripts/providers/phaxio/setup.sh
      - Tools → Scripts & Tests → “Configure Phaxio callbacks + HMAC” (admin‑only)
  - 5‑minute Quickstart rework
      - Update Setup Wizard for Phaxio/Sinch simplified fields + Verify buttons
      - Add “Send Test Fax” text‑to‑PDF pane
  - Docs single “Start in 5 Minutes” page
      - Draft and link from Setup Wizard “Learn more”
  - Hosted mode scaffolding (behind feature flag)
      - Tenant column on jobs, limit table, API key scope “consumer:send”
      - Stripe stubs (webhook endpoint, plan enum) and quotas in UI
  - Email receipt after send (Lite & enterprise optional)
      - SMTP/SES config + template