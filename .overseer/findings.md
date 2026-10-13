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

*Next update: Oct 13, 2:30 PM MT (hourly heartbeat)*
