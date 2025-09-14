# V3 Admin Console GUI Implementation - Summary

## The Reality
After re‑checking the actual Admin UI and API:
- Admin UI is already complete across major tabs (Dashboard, Send, Jobs, Inbox, Keys, Setup, Settings, MCP, Logs, Diagnostics, Plugins)
- API endpoints for admin operations exist and are wired to the UI
- v3 Plugins endpoints exist and are feature‑gated
- The embedded MCP servers (Python SSE/HTTP) are mountable under the FastAPI app and controlled by env flags
- We need targeted enhancements, not rewrites

Recently implemented in this pass:
- Feature flag wiring for `FEATURE_V3_PLUGINS` and `FEATURE_PLUGIN_INSTALL` (UI apply + backend PUT + env export)
- Expanded Dashboard with Config Overview, MCP Overview, Plugins (feature‑gated), and SDK Quickstart cards
- Plugins tab: non‑secret configuration dialog and “Set Active” preserved
- Plugin Builder: Python and Node scaffolds with correct return shapes

## The Plan (7 Phases, ~23 hours total)

### Phase 1: Feature Flags Wiring (1 hour)
The Settings page already shows toggles. Wire them through the backend so they persist in‑process and export correctly:
- Update `/admin/settings` PUT to set `FEATURE_V3_PLUGINS` and `FEATURE_PLUGIN_INSTALL`
- Ensure export `.env` includes both flags; `FAX_DISABLED` and `INBOUND_ENABLED` are already handled
- Keep “restart recommended” hints when flags affect mounted sub‑apps

### Phase 2: Plugin Configuration (non‑secret) (3 hours)
- Enhance Plugins tab with a lightweight config drawer for non‑secret settings only
- Outbound provider selection persists to `config/faxbot.config.json` via `/plugins/{id}/config`
- Secrets (e.g., Phaxio/Sinch credentials, AMI password) remain in Settings → `.env` only (do not store in plugin config)
- Storage plugin (S3) form: `bucket`, `region`, `prefix`, `endpoint_url`, `kms_key_id`

### Phase 3: MCP Tab Refinements (2 hours)
- Keep existing component; add copy‑to‑clipboard UX polish and health refresh
- Clarify it controls embedded Python MCP servers mounted under `/mcp/sse` and `/mcp/http`
- No Node server steps required; toggle flags and confirm health at `/mcp/*/health`

### Phase 4: Plugin Builder (Node + Python) (5 hours)
- Generate provider plugin skeletons for both Node.js and Python (outbound/storage)
- Use dev kits: `@faxbot/plugin-dev` (Node) and `faxbot_plugin_dev` (Python)
- Provide download of the scaffold and link to docs on how to install locally (remote install remains disabled by default)

### Phase 5: Expanded Dashboard (3 hours)
- Add a “Config Overview” card sourced from `/admin/config` (backend, storage, security posture, v3 plugins flag, active outbound plugin when enabled)
- Add an “MCP Overview” card sourced from `/admin/config.mcp` (SSE/HTTP enabled flags, paths, OAuth required)
- Add a “Plugins” card (when `FEATURE_V3_PLUGINS=true`) with active outbound and counts from `/plugins`
- Add an “SDK & Quickstart” card with copyable install lines and sample client configs (Node/Python 1.0.2), plus base URL and auth header
- Keep existing health/job counters; avoid heavy queries and live telemetry

### Phase 6: Curated Registry (1 hour)
- Use `/plugin-registry` items directly; add a simple text filter
- Link “Learn more” per plugin; avoid generic NPM/PyPI browsing by default

### Phase 7: HIPAA Warnings (2 hours)
- Reusable warning component added to Jobs, Logs, Inbound, Settings
- Keep copy short and link to docs for deeper guidance

## Key Decisions

### What We're Building
✅ GUI for ALL v3 features
✅ Plugin builder wizard (Node and Python providers)
✅ Simple, practical solutions
✅ HIPAA compliance warnings

### What We're NOT Building
❌ Docker management (use Portainer)
❌ Plugin marketplace (against FOSS)
❌ Plugin marketplace (against FOSS)
❌ Complex monitoring (use Grafana)
❌ User management system

## For the Implementing Agent

### Start Here
1. Read `practical_gui_plan.md` - Updated to match current code
2. Start with Phase 1 - Wire flags end‑to‑end
3. Test after EACH phase
4. Update `v3_admin_console_thoughts.md` after each phase

### File Locations
- Components: `api/admin_ui/src/components/`
- API Client: `api/admin_ui/src/api/client.ts`
- Main App: `api/admin_ui/src/App.tsx`
- Backend: `api/app/main.py`

### Testing Commands
```bash
# Start backend (ensures /admin UI is served when enabled)
cd api && ENABLE_LOCAL_ADMIN=true python -m app.main

# Build UI
cd api/admin_ui && npm install && npm run build

# Open browser
http://localhost:8080/admin
```

### If Something Breaks
```bash
# Revert specific file
git checkout HEAD -- api/admin_ui/src/components/Settings.tsx

# Or full UI rollback
git checkout HEAD~1 -- api/admin_ui/
```

## Success Criteria

The user should be able to:
1. **Enable v3 features** in UI with proper backend wiring ✅
2. **Configure plugins** via forms ✅
3. **Build new plugins** with Python wizard ✅
4. **Copy MCP configs** easily ✅
5. **See real metrics** on dashboard ✅
6. **Browse curated plugins** via registry ✅

## Remember

- **This is PRODUCTION** [[memory:8895091]] - Active BAAs, real PHI
- **GUI-first mandate** - Everything via browser after `docker compose up`
- **Junior dev audience** - Clear, step-by-step instructions
- **KISS, DRY, YAGNI** - Don't overcomplicate
- **Node.js = Python** [[memory:8895543]] - SDKs parity; dev kits first‑class for both

## The Bottom Line

**23 hours of work to make EVERYTHING accessible via GUI.**

No more terminal commands except `docker compose up`.

This is what the user asked for. This is what we're delivering.
