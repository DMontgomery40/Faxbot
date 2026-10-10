import { describe, expect, it } from 'vitest';
import type { Capabilities, Capability } from '../api/capabilityTypes';
import fixture from './capabilities.json';

// One answer of GET /routing/capabilities, evaluated on a real synthetic installation
// (api/tests/test_capabilities.py). The Capabilities page, the Overview and `faxbot costs capabilities` read it.
const answer: Capabilities = fixture.response as unknown as Capabilities;
const items: Capability[] = answer.outcomes.flatMap((outcome) => outcome.capabilities);

describe('The capabilities read contract', () => {
  it('lists every capability once, by outcome, with its own address and no money', () => {
    const keys = items.map((item) => item.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(answer.outcomes).toHaveLength(7);
    for (const outcome of answer.outcomes) {
      for (const item of outcome.capabilities) {
        expect(item.outcome).toBe(outcome.key);
        expect(item.address).toBe(`savings/capabilities?key=${item.key}`);
      }
    }
    expect(JSON.stringify(answer)).not.toMatch(/\$|USD|"amount"|micros/);
  });

  it('carries every state the page filters on, and marks only ready capabilities as next improvements', () => {
    const filters = answer.filters.map((filter) => filter.key);
    expect(filters).toEqual(['on', 'off', 'ready', 'needs', 'experimental']);
    for (const key of filters) expect(items.some((item) => item.filters.includes(key))).toBe(true);
    for (const item of items) {
      expect(item.improvement === null).toBe(!item.ready);
      expect(item.missing).toBe(item.prerequisites.filter((prerequisite) => !prerequisite.met).length);
      // "Not here" always names what is missing.
      if (!item.works.here) expect(item.missing).toBeGreaterThan(0);
    }
  });
});
