# Faxbot Test Campaign Overseer — Orientation

**Campaign Window:** Oct 13, 2026, ~7:30 AM–5:30 PM MT  
**Status:** Initial orientation run (1:32 PM MT)

## What's Running

Claude (another agent with leads/subagents) is running stacked-mechanism tests through FAXBEEP (a public fax-to-web service, listed in `api/app/public_test_lines.py`). The test harness is called FAXBEEP-based delivery validation.

**Key mechanisms being tested:**
- Savings route selection and cost estimation (new in ent/savings-map)
- Measured page encoding/codec performance (deterministic under CI load: test/joint-optimizer-deterministic merged)
- SSL Fax engine header lines and dense-page timing
- Direct delivery and case-packet reuse (future enterprise direction)

**Test approach:**
- Send real faxes through selected providers to Faxbeep's public test lines
- Verify delivery success via Faxbeep's public API (`faxbeep.com/api/faxes` lookup by slug)
- Cross-check page counts, encoding type, and timing against expected stacked-mechanism results
- Measure byte reduction and milliseconds per page; extrapolate to cost/time baseline

**CI/main integration:**
- All test branches in the last 24h merged successfully (total 103 PRs merged on main)
- Recent test PRs: test/recommendations-empty-findby, test/joint-optimizer-deterministic, test/combo-gives-back
- No active test branches on Oct 13 yet; Claude's work will land here after testing

## Where Results Land

1. **Test branches:** Claude pushes to `test/*` and `ent/*` branches; leads/subagents may create more
2. **GitHub PRs:** Test results appear in PR descriptions and check runs (GitHub Actions CI logs)
3. **Faxbot's own logs:** In `.faxdata/` (local test run) or provider delivery records (live Faxbeep receipts)
4. **Claude's communication:** Look for `.claude/` folder or root scratchpad files (not yet active)

**Result validation ground truth:**
- David's own logs (Faxbeep API receipts, timing, page counts) are definitive
- CI test output: `api/backend-results.xml` uploaded as artifact per shard
- Manual verification: Faxbeep's public page shows each received fax for 30 days

## How to Message Claude's Lead

**File location for leads:** This file (`.overseer/orientation.md`) and `.overseer/findings.md` (not yet created)  
**Channel:** No existing `.claude/` subfolder yet; expect one to appear when Claude's lead first communicates back  
**What to do:**
- **Silent by default:** Monitor cheap diffs each heartbeat; only intervene for:
  - Badly wrong test interpretation (e.g., treating 13% auto baseline as a weakness vs. enterprise ROI design)
  - Stacking tests that don't actually stack (e.g., codec + timing reported separately instead of combined)
  - Unsourced claims or claims contradicting David's ground truth
  - Broken test harness (e.g., faxes not sent, Faxbeep lookup failing, CI artifact missing)
  - Wasted spend (e.g., repeated same test, wrong provider, wrong country)
  - Major oversight (e.g., missing mechanism, wrong test order)
- **Format when intervening:** Short, specific, one-sentence action per problem

**Context reminders:**
- Faxbot is a cost-saving sidecar: never blocks faxes, fails open
- Savings = measured pages + milliseconds, then extrapolated to cost per attempt
- Default/auto ~13% is the deliberate enterprise ROI baseline (documented in console and README)
- Test harness ground truth = David's logs (Faxbeep API, provider receipts, timing)

## Repo Map

```
/workspace/
├─ api/
│  ├─ app/public_test_lines.py         ← FAXBEEP line list & API lookup
│  ├─ routing/                          ← Route selection, cost estimates, mechanisms
│  ├─ pages/coding.py                  ← Codec/encoding performance
│  ├─ tests/test_savings_mechanisms.py  ← Stacked mechanism checks
│  └─ ...
├─ .github/workflows/ci.yml             ← CI shards 1-8, synthetic-only enterprise checks
├─ planning/
│  ├─ 2026-10-02-faxbot-durable-outbound.md
│  ├─ 2026-10-02-faxbot-schema-foundation.md
│  └─ enterprise-correspondence.md      ← E0–E4 foundations & acceptance criteria
├─ README.md                            ← Roadmap (source of truth for implemented vs. planned)
└─ AGENTS.md                            ← Lead/agent communication rules & research evidence

Claude's WIP backups (Oct 8):
  - 3e5fbadf WIP: ent/savings-map      ← Savings visualization (paused)
  - a0b84503 WIP: ent/polled-transmit
  - ... (4 more)
```

## Key Recent Commits (Oct 11–13)

1. **test/recommendations-empty-findby** — Console UI wait-state under CI load
2. **docs/ai-update-38194266895** — Lead's review of Docs Autopilot proposals
3. **test/joint-optimizer-deterministic** — Measured accounts deterministic (Oct 11)
4. **fix/settled-backfill** — Certainty handling (Oct 11)
5. **perf/predict-speed** — 30-page estimate: 29s → 12s same answer (Oct 10)

All merged to main; no breaking changes or regressions visible.

## Next Heartbeat Checklist

Each hour, check:
1. New commits to `main` or `test/*` / `ent/*` branches
2. New GH PR activity, CI status
3. `.overseer/findings.md` or `.claude/` for lead communication
4. Token budget (stay under 1k per heartbeat diffs)
5. Note any gaps or odd readings in local findings.md

---
*Orientation complete, 2026-10-13 13:32 MT. Ready for hourly heartbeats.*
