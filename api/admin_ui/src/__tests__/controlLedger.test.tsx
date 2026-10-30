// #64 source preservation gate. A separate browser walkthrough must prove that
// controls are usable at desktop/phone widths and with the keyboard.
import { describe, expect, it } from 'vitest';
import { NAVIGATION, pageAddress, resolvesTo, resolveHash, visibleNavigation } from '../navigation';
import ledger from './controlLedger.json';

const denied = visibleNavigation(new Set(), { send: false, jobs: false, inbox: false, work: false }, { pluginsEnabled: true });

describe('the #46 operational and settings control-family ledger', () => {
  it('accounts for each of O01–O36 and S01–S36 exactly once', () => {
    const expected = ['O', 'S'].flatMap(prefix => Array.from({ length: 36 }, (_, i) => `${prefix}${String(i + 1).padStart(2, '0')}`));
    expect(ledger.controls.map(row => row.id)).toEqual(expected);
  });

  for (const row of ledger.controls) {
    it(`${row.id}: ${row.home} retains its canonical entry page and gate`, () => {
      expect(row.family.trim()).not.toBe('');
      expect(row.sources.length).toBeGreaterThan(0);
      const destination = resolvesTo(row.home);
      expect(destination, row.id).not.toBeNull();
      expect(pageAddress(destination!.area.id, destination!.page.id)).toBe(`#/${row.home}`);
      expect(destination!.page.gate).toEqual(row.gate);
      expect(typeof destination!.page.render).toBe('function');
      if (Object.keys(row.gate).length) expect(resolveHash(denied, `#/${row.home}`)?.kind).toBe('forbidden');
    });
  }

  it('rejects a removed home even while its component source remains', () => {
    const row = ledger.controls[0];
    const navigation = NAVIGATION.map(area => ({ ...area, pages: area.pages.filter(page => pageAddress(area.id, page.id) !== `#/${row.home}`) }));
    expect(resolveHash(navigation, `#/${row.home}`)?.kind).not.toBe('page');
  });
});
