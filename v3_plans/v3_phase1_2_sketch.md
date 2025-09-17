# Faxbot v3 — Phase 1 & 2 Sketchy Items

Purpose: Track items identified as higher‑risk or requiring broader design alignment before implementation.

## Runtime Config Application
- Risk: Overriding env‑based safety and PHI policies unintentionally.
- Decision: Persist JSON only in Phase 2; no runtime apply. Add explicit Admin UI flow later with “apply + restart” semantics and clear HIPAA warnings.

## Admin Scopes for Plugins
- Risk: Introducing `admin:plugins:*` requires DB scope model updates, key issuance UX, and UI changes.
- Decision: Gate plugin endpoints behind existing `require_admin` for now. Scope expansion deferred to Phase 3+.

## Dynamic Plugin Installation
- Risk: Supply chain, binary deps, sandboxing, HIPAA posture.
- Decision: Keep `FEATURE_PLUGIN_INSTALL=false`. If enabled in the future, require allowlists, checksums/signatures, and no network by default.

## WebSocket Transport
- Risk: Out of alignment with AGENTS.md (MCP uses stdio/HTTP/SSE). Adds surface area for auth and CORS.
- Decision: Not part of Phases 1–2. Consider later only if justified.

## JSON Schema Validation
- Risk: Accepting arbitrary settings may lead to misconfiguration.
- Decision: For Phase 2, writes are allowed but not applied to runtime; add schema validation before enabling live config changes.

