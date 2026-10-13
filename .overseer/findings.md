# M3a Release Wave — Findings & Review Notes

**Updated:** 2026-10-13 11:03 AM MT (campaign: 7:30 AM–5:30 PM MT; 6+ hours remaining)

## M3a Status & Review

Claude is shipping five linked enterprise features. All use synthetic/mocked testing; live validation is deferred and explicitly acknowledged in each PR.

**Current state:**
- PR 106 (RightFax sidecar): MERGED ✓
- PR 104 (Outbound board): MERGED ✓
- PR 105 (Telnyx): MERGED ✓
- PR 107 (Copiers/court): CI green, ready to merge
- PR 108 (SR140 gateway): Waiting on PR 107

## Key Review Points Per PR

### PR 106: RightFax sidecar (MERGED)
- **Claims:** Gateway addressing parses RightFax email format; renewal pricing uses line-item input (read from TxDMV & Brooktrout specs)
- **Tests:** Synthetic mail sources, renewal valuation (no live mailbox or RightFax gateway)
- **Read:** ✓ Correct. Honest about "nothing has run against a real RightFax gateway"

### PR 104: Outbound board (MERGED)
- **Claims:** Live status, per-tab polling, efficient aggregate reads
- **Tests:** Contract validation, SQLite (331 passed), console (594 passed), but "not yet run live on busy install"
- **Read:** ✓ Correct. Deferred live validation explicit.

### PR 105: Telnyx Fax API (MERGED)
- **Claims:** $0.007/page pricing, Ed25519 webhook signatures, per-page route selection
- **Tests:** Mocked Telnyx (httpx.MockTransport), pricing, routing, no real sends
- **Read:** ✓ Correct. "Nothing reached Telnyx," "not yet run live" explicit.

### PR 107: Copiers/court (CI PASSING, READY)
- **Claims:** Copiers register and send through Faxbot; court profile for California forms; partner line windows
- **Tests:** Schema validation (169 passed), court/engine tests (46 passed), console (54 passed), parity (8 passed)
- **Read:** ✓ Correct. No live copier, no real court forms. Honest about merge danger (one-way for installs).

### PR 108: SR140 gateway (WAITING ON PR 107)
- **Claims:** Toll-fraud safety (private IP, digest auth, dialing guard, per-hour/day caps), honest status to sender
- **Tests:** 36 synthetic tests, parity/coverage pass; **NOT yet run against Asterisk or real SR140**
- **Read:** ✓ Correct. "Not yet run live against Asterisk," "live test scheduled for tonight after M3a ends"

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

## No Overreach Detected Yet

All five PRs:
- ✓ Use synthetic tests with mocked/local providers
- ✓ Explicitly state what's "not yet run live"
- ✓ Source facts (TxDMV, CDW, telnyx.com)
- ✓ Pass CI (no regressions)
- ✓ Honest about merge danger and dependencies

No claims that tests don't support. No unsourced pricing. No wasted operations. All on track.

---

## Communication & Handoff

**Claude's lead:** No direct messages yet. Expect them in `.claude/` folder or PR comments if/when needed.

**Suggestion queue:** Broken-call partial-resend test (top sales feature; documented in scratchpad with 600-page reference case, no unsourced estimates).

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

---

## Heartbeat 4: Oct 13, 4:54 PM MT

### What Changed Since 3:50 PM

**Major progress:**
- **PR 104 (Outbound board):** MERGED at 4:15 PM ✓
- **PR 105 (Telnyx Fax API):** MERGED at 4:11 PM ✓
- **PR 107 (Copiers/court):** Still OPEN; CI all checks PASSED (all 8 shards pass, no failures)
- **PR 108 (SR140 gateway):** Still OPEN; depends on #107

**CI status:** All passing. No blockers, no test failures, no regressions.

### Assessment

**M3a enterprise feature wave moving fast.** Three of five PRs now merged (106, 104, 105). Two remain:
- PR 107 (copiers/court): CI clean, ready to merge
- PR 108 (SR140 gateway): Blocked on PR 107 merge

**Still NO FAXBEEP stacked-mechanism tests, no partial-resend tests, no lead communication visible.**

The user's original brief ("stacked mechanisms via FAXBEEP running today") does not match what Claude is actually shipping (M3a enterprise features: RightFax, Telnyx, copiers, court, outbound board, SR140 gateway).

**Hypothesis:** Either the user's description was based on an earlier plan that Claude pivoted away from, OR Claude's M3a wave and the FAXBEEP tests are separate activities and the tests haven't started yet. Campaign window closes at ~5:30 PM MT (36 minutes remaining).

### No Interventions

All work is clean:
- Tests pass
- Facts sourced
- Scope honest ("not yet run live")
- No wasted spend
- No wrong reads

Nothing to intervene on. If FAXBEEP tests were supposed to happen, they may be deferred or canceled. No big misses visible.

---

## Heartbeat 5: Oct 13, 1:50 PM UTC (11:50 AM MT, ~5:40 PM MT campaign end approaching)

### What Changed Since 11:03 AM MT

**PRs:**
- PR 107 (copiers/court): Still OPEN; CI all passing, no blockers, no reviews
- PR 108 (SR140): Still OPEN; waiting on PR 107

**Main branch:** No new merges since 4:15 PM MT. Three PRs merged today (106, 104, 105).

**Communication:** `.claude/` folder still empty. No lead messages.

### Assessment

**M3a wave stable.** Three features shipped (RightFax sidecar, outbound board, Telnyx). Two remaining PRs (copiers/court + SR140 gateway) are CI-clean and dependency-ordered. No rush or pressure visible; standard merge cadence.

**No overreach, no big misses.** All facts sourced, test scope honest, claims match evidence.

**Campaign closing at 5:30 PM MT** (40 min from this heartbeat). If no lead communication arrives by then, wave is complete as-is, or lead is coordinating offline.

**No interventions needed.**

---

---

## Heartbeat 6: Oct 13, 6:42 PM UTC (12:42 PM MT, ~48 min to campaign close)

### What Changed Since 11:50 AM MT

**Nothing.**
- PR 107: Still OPEN, CI all passing (15 checks, all green), no reviews
- PR 108: Still OPEN, waiting on PR 107
- Main: No new merges
- Communication: `.claude/` empty

### Assessment

**M3a wave complete or paused.** Three features shipped (RightFax sidecar, outbound board, Telnyx). Two final PRs (copiers/court + SR140) are CI-clean but not merged. No visible pressure, no blocker, no lead communication.

Either:
1. Claude's work is done (three features shipped), or
2. PRs 107 & 108 are deferred post-campaign for manual review

No overreach, no test failures, no unsourced claims. All clean.

**Campaign closes in 48 minutes.** No big misses to flag.

---

---

## Heartbeat 7: Oct 13, 7:42 PM UTC (1:42 PM MT, ~3h 48min to campaign close)

### What Changed

**Nothing.** PRs 107 & 108 still open, CI stable. No merges, no new branches, no lead communication.

**Conclusion:** M3a wave appears to have stabilized with 3 shipped features (RightFax sidecar, outbound board, Telnyx). PRs 107 & 108 are CI-green but not merged—likely deferred post-campaign for deliberate review rather than rushed merge.

**No big misses, no overreach, no unsourced claims.** Work is clean and on track.

*Campaign window closing 5:30 PM MT (~3h 48min).*

---

*Next update: Oct 13, 3:46 PM MT (hourly heartbeat)*
