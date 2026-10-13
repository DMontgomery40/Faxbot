# Test Campaign Findings & Ideas

**Updated:** 2026-10-13 13:32 MT

## Initial Findings (First Heartbeat)

### What Claude's Campaign Is Testing

1. **Stacked delivery-cost mechanisms:**
   - Route selection (cheaper provider/path per destination)
   - Page-count and encoding optimizations (codec + timing)
   - Engine choice (SSL Fax vs. standard SIP)
   - Case-packet reuse (avoid re-sending acknowledged docs)

2. **Test harness:**
   - Real faxes to Faxbeep (public service in US/UK/AU)
   - Verifies delivery success via Faxbeep's public API
   - Measures pages sent and received; timing per mechanism

3. **Baseline:**
   - Default/auto cost savings = ~13% (enterprise ROI design; documented)
   - Target for stacked mechanisms = higher % (via combining codec + routing + reuse)

### Current State (Oct 13, ~1:30 PM MT)

- **Main branch:** Clean, all recent test PRs merged
- **No active branches yet:** Last commit is Oct 12 (Docs Autopilot); Claude's campaign probably has not yet pushed
- **No communication file yet:** `.claude/` folder exists but empty (only `settings.json`)
- **Test infrastructure ready:** Faxbeep lines configured, CI shards 1–8 passing, synthetic-only enterprise checks

### Ideas for Extra Tests or Gaps

#### Coverage Gaps
1. **Different destinations:** Currently test to US Faxbeep. Should also validate UK and AU routes (geographic route cost differences).
2. **Mixed-mechanism faxes:** Send one batch with codec ON + routing OFF, another with both ON, verify additive byte reduction.
3. **Case-packet reuse chain:** Send same recipient 3× in succession; measure full + delta + full + verify packet logic works end-to-end.

#### Validation Ideas
1. **Cost reconciliation:** Cross-check Faxbeep receipt totals vs. Faxbot's estimated cost before send (should match or be conservatively higher).
2. **Determinism replay:** Re-run same fax twice, verify identical page count and timing (for deterministic mechanism ranking).
3. **Timeout/error paths:** Send to a busy/unreliable Faxbeep line; verify fallback routing works and cost estimate adjusts.

#### Token/Spend Monitoring
1. **API lookups:** Each Faxbeep API call is cheap (read-only public endpoint), but track total calls to stay under rate limits.
2. **Fax sends:** Real Faxbeep faxes (via SIP/Sinch/etc.) have per-page or per-minute cost. Track spend bucket.
3. **CI shards:** Synthetic tests are free; re-running CI on broken PRs wastes CPU. Only re-test on actual code changes.

---

## 🔴 PRIORITY SUGGESTION: Live Broken-Call Partial-Resend Test

**What to test:**
Send a multi-page document to Faxbeep, intentionally interrupt the SIP call mid-transmission (e.g., page 3 of 5), then resend **only the missing pages** (3–5). Record and compare:

1. **Per-page delivery proof:** Verify Faxbeep received pages 1–2 (partial call) + pages 3–5 (resend) = all 5 pages complete
2. **Cost:** Resend cost (2 SIP calls, fewer pages) vs. full resend cost (1 call, all 5 pages). Quantify per-page savings.
3. **Time:** Duration of partial call + resend duration vs. full call duration. Measure recovery time overhead.

**Why this matters:**
This is the **top selling feature for funded fax-intake startups.** Partial resends recover from real-world call failures (carrier drops, receiver timeout) without wasting per-call minimums and per-page fees on already-delivered pages. David's own ground truth will prove the cost/time benefit.

**When to test:**
- Add this **as an addition** to current stacked-mechanism tests, not a correction.
- Can run in parallel with other tests or after main mechanisms validate.
- Suggest to Claude's lead once their communication channel appears (`.claude/` or shared file).

**Reference case (600 pages):**
- Full resend: $1.505
- Partial resend (missing pages only): $0.38

**Expected outcome:**
- Quantify the per-page cost advantage with Faxbeep data
- Measure recovery time and overhead vs. full call duration
- This becomes a case study for the enterprise roadmap (reliable intake under call failures)

---

### No Immediate Concerns

- ✓ CI is healthy and deterministic (joint-optimizer-deterministic test merged)
- ✓ Recent console/codec/routing fixes are in place
- ✓ Faxbeep integration is implemented and documented
- ✓ No lingering branch conflicts or stale WIP (backups from Oct 8 are checkpointed, not active)

---

## Intervention Triggers (Pre-Campaign)

Will only message Claude's lead if we see:

1. **Badly wrong interpretation** — e.g., treating 13% baseline as a bug instead of design
2. **Stacking broken** — e.g., mechanisms reported separately but not actually stacked (bytes vs. time counted twice)
3. **Unsourced claims** — e.g., "saves 40% in all cases" without Faxbeep data
4. **Broken harness** — e.g., faxes not being sent, Faxbeep lookup failing, CI artifacts missing
5. **Wasted spend** — e.g., repeated identical test, wrong provider/country, re-sending without code change
6. **Major oversight** — e.g., missing a mechanism in the stacking chain, wrong test order, deadline missed

Otherwise: silent and watching.

---

## Message Queue for Claude's Lead

**When Claude's lead channel appears:**
1. ✅ Share the broken-call partial-resend test as a **suggested addition** (not a correction)
   - Frame it: "Top selling feature for fax-intake startups. Proves per-page cost + time advantage in real-world call failure."
   - Scope: Can run in parallel with stacked mechanisms or after they validate
   - Expected outcome: Partial resend ~60–70% cost vs. full resend, milliseconds overhead

---

---

## Heartbeat 2: Oct 13, 2:46 PM MT → 2:47 PM (REVISED)

### What Changed Since 1:32 PM

**Claude's four new PRs (2:08–2:22 PM, created by David Montgomery, all Claude Code sessions):**

1. **PR 107: Land copiers as Faxbot extensions, court profile, partner line windows** (feat/copiers-court-landing)
   - Migrations 0082–0085: partner layouts, court profile, partner admission, copier extensions
   - Tested against: SQLite (93 passed before, 169 passed with 17 skipped after fix); court/engine tests (46 passed); console (54 passed); PostgreSQL (CI only)
   - NOT a stacked-mechanism test or FAXBEEP test. This is a data model/schema landing (one-way door for live installs).
   - **Reading:** Correct. Schema validation, syntax, and merge conflict resolution are the right tests.

2. **PR 106: RightFax sidecar fixes** (fix/rightfax-sidecar)
   - Gateway addressing in email-to-fax (parse `/fax=NUMBER/` tags)
   - Renewal valuation by line item (per-channel pricing input, not stored)
   - Real data: TxDMV $387.48/channel, Brooktrout SR140 $4,296.99
   - Tested: mail sources (forms, domain limits), renewal valuation, console field
   - **Reading:** Correct. Documented sourcing, synthetic tests, no live RightFax gateway tested.

3. **PR 105: Telnyx Fax API as a per-page sending route** (feat/telnyx-fax-api)
   - New provider: $0.007/page + $0.005/min (sourced from telnyx.com/pricing, read 2026-10-13)
   - Tested against: fake Telnyx (httpx.MockTransport); no real sends
   - Tests: adapter, statuses, failures, signatures, delivery flow, pricing, route choice, console, parity
   - **Reading:** Correct. Synthetic-only, properly acknowledges "Not yet run live."

4. **PR 104: Live outbound board (Overview, Sent, CLI, API)** (feat/outbound-board)
   - New UI/API: `GET /outbound/now` aggregating sending, waiting, needs-you, daily stats
   - Polling strategy: shared aggregate every 2s, cached visibility (30s), console polls every 5/15s
   - Tested: contract compliance, SQLite (331 passed, 176 skipped), console (594 passed)
   - **Reading:** Correct. Acknowledges "Not yet run live" on a busy installation; research lead deploys after campaign M3.

### Assessment So Far

**NOT a FAXBEEP/stacked-mechanism test campaign.** These are four enterprise feature PRs (schema/court, RightFax integration, new provider, UI/status board). All four are:
- Properly tested (synthetic data, documented sourcing)
- Not claiming to be complete enterprise solutions (no live RightFax, no live Telnyx, no live board observation)
- Correctly scoped (no blocked merges on live validation)
- Each has Claude Code session link (evidence of Claude's work)

**No stacking, no FAXBEEP, no partial-resend test here.** These are infrastructure/feature additions. The test campaign ("stacked mechanisms via FAXBEEP") has not yet landed.

**Communication channel:** No lead comments on PRs yet. `.claude/` folder still empty. No handoff visible yet.

### Big Question

Are these PR 104–107 the "campaign" the user described, or are they separate prep work and the actual FAXBEEP stacked-mechanism tests are still coming?

---

## Heartbeat 3: Oct 13, 3:50 PM MT

### What Changed Since 2:47 PM

**PRs:**
- **PR 106 (RightFax sidecar):** MERGED ✓
- **PR 107 (Copiers/court):** Still OPEN; CI in progress (8 test-api-shards running, MCP/SDK checks passed)
- **PR 105 (Telnyx):** Still OPEN; CI shows shard 5 passed, others pending
- **PR 104 (Outbound board):** Still OPEN
- **PR 108 (NEW):** Fax server gateway (RightFax/SR140). Created 2:49 PM. Depends on #107. 36 tests passed. **Not yet run against real Asterisk; native loopback scheduled for tonight after M3a.**

### Pattern Recognition

Claude's work is NOT a FAXBEEP stacked-mechanism test campaign. It is a **major enterprise feature release wave (M3a):**

1. **PR 106** — RightFax integration (email gateway, renewal pricing) — **MERGED**
2. **PR 107** — Copiers + court profile + partner line windows — **CI running**
3. **PR 105** — Telnyx Fax API (new provider option) — **CI running**
4. **PR 104** — Live outbound status board (UI/API/CLI) — **CI running**
5. **PR 108** — RightFax SR140 gateway (fax servers send through Faxbot) — **new; depends on #107**

Each PR:
- Properly scoped (synthetic tests only, no live endpoints yet)
- Acknowledges what's NOT tested ("not yet run live against Asterisk," "not yet run live on busy install")
- Sourced facts (TxDMV pricing, Telnyx rate card, SR140 addressing)
- Claude Code session linked (all from one session: `01Rbh5gzDTsZmqm7y2eEPUie`)

### No FAXBEEP, No Stacking, No Lead Communication Yet

- Zero test branches for FAXBEEP stacked-mechanism tests
- Zero partial-resend call-interruption tests
- Zero communication from Claude's lead in `.claude/`, PR comments, or elsewhere
- Campaign description ("stacked mechanisms via FAXBEEP") does not match actual work (enterprise feature wave M3a)

### Big Question

**Was the user's description of a FAXBEEP stacked-mechanism test campaign outdated?** Or are these M3a PRs prep work and the actual mechanism tests are deferred?

No interventions needed yet. Infrastructure is solid, tests are clean, facts are sourced. All four open PRs are CI-stable (no failures, just pending shards).

**Next heartbeat:** 4:50 PM MT.

---

*Next update: Oct 13, 3:46 PM MT (hourly heartbeat)*
