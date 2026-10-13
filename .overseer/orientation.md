# Faxbot Overseer — M3a Enterprise Release Wave

**Campaign Window:** Oct 13, 2026, ~7:30 AM–5:30 PM MT  
**Current Time:** 11:03 AM MT (6+ hours remaining)  
**Status:** Monitoring Claude's M3a enterprise feature release

## What's Running

Claude (another agent with leads/subagents) is shipping a major enterprise release wave (M3a). Work includes infrastructure, provider integrations, operational UIs, and gateway support:

**Features in flight:**
1. RightFax sidecar: email gateway addressing, renewal line-item pricing — **MERGED**
2. Copiers + court profile + partner line windows (schema 0082–0085) — **CI passing**
3. Telnyx Fax API: per-page billing route — **MERGED**
4. Live outbound board (Overview/Sent UI, API, CLI status) — **MERGED**
5. RightFax/SR140 gateway: fax servers send through Faxbot (schema 0086) — **in progress**

All work: synthetic tests only, live validation deferred. Facts sourced. Scope honest about what's "not yet run live."

## Where Results Land

1. **GitHub PRs:** Test coverage in PR descriptions, CI checks (GitHub Actions)
2. **Main branch:** Merges land and ship
3. **Claude's communication:** Expect `.claude/` folder or PR comments when lead needs to sync

## What to Monitor

**Flag only BIG MISSES:**
- **Claims overreach:** PR says "safety" but tests are synthetic-only, never live
- **Unsourced facts:** Pricing, timing, or capability claims without documented source
- **Broken harness:** CI fails, tests missing, artifacts not uploaded
- **Wasted spend:** Duplicate work, wrong test scope, unnecessary operations
- **Major oversight:** Blocked merge, schema dependency broken, missing validation

**Don't flag:**
- Incomplete live validation (expected and acknowledged)
- Deferred features (OK if honest about scope)
- PR descriptions being long or detailed (more info is better)

## Intervention Format

If intervening: short, specific, one-sentence per issue. Use `.claude/` or PR comment. Flag what to fix, not what was built.

---

*Orientation complete, 11:03 AM MT. Ready for heartbeats.*
