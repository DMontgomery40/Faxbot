import { describe, expect, it } from 'vitest';
import type { FaxJob } from '../api/types';
import { CANT_TELL_LABEL, deliveryNotice, jobStatusLabel, routeText } from '../components/JobsList';

const uncertain = (overrides: Partial<FaxJob> = {}): FaxJob => ({
  id: 'c'.repeat(32), to_number: '+15550100002', status: 'in_progress', backend: 'humblefax',
  created_at: '2026-10-10T20:45:00', updated_at: '2026-10-10T20:47:00', delivery_state: 'reconciliation_required',
  dispatch_mode: 'normal', delivery_version: 4, ...overrides });

describe('Sent: a fax whose result is uncertain', () => {
  it("says the server's sentence for the route that carried it", () => {
    // Over your own trunk or fax engines there is no provider account to check.
    const ownCall = 'The call ended before the receiving fax machine confirmed the pages; check with the recipient '
      + 'before sending this fax again.';
    expect(deliveryNotice(uncertain({ reconciliation_reason: ownCall }))).toBe(ownCall);
    const service = 'Faxbot could not confirm delivery; check your provider account before sending this fax again.';
    expect(deliveryNotice(uncertain({ reconciliation_reason: service }))).toBe(service);
  });

  it('falls back to a sentence true for any route when the server gives none', () => {
    expect(deliveryNotice(uncertain())).toBe("Delivery couldn't be confirmed; check with the recipient, or in your "
      + 'provider account if a fax service sent it, before sending it again.');
    expect(deliveryNotice(uncertain({ dispatch_mode: 'legacy' }))).toBe('Imported fax with no delivery record; check '
      + 'with the recipient, or in your provider account if a fax service sent it.');
    expect(deliveryNotice(uncertain({ delivery_state: 'success' }))).toBeNull();
  });
});

describe('Sent: a fax a person settled as can\'t tell', () => {
  // outbound_store.CANT_TELL; the command line uses the same label and sentence (cli/commands/fax.py).
  const sentence = "Settled as can't tell: it may have arrived, so check before sending again.";
  const settled = uncertain({ status: 'failed', delivery_state: 'failed', error: sentence, settled_cant_tell: true });

  it('reads "Can\'t tell" with the settled sentence, never a bare "Failed"', () => {
    expect(CANT_TELL_LABEL).toBe("Can't tell");
    expect(jobStatusLabel(settled)).toBe("Can't tell");
    expect(deliveryNotice(settled)).toBe(sentence);
    // A fax that really failed still reads Failed, with no notice.
    expect(jobStatusLabel({ ...settled, settled_cant_tell: false })).toBe('Failed');
    expect(deliveryNotice({ ...settled, settled_cant_tell: false })).toBeNull();
  });
});

describe('Sent route', () => {
  it('names the route that carried the fax, and any route tried before it', () => {
    expect(routeText('humblefax', { state: 'none', summary: null, reported_cost: [], routes: ['humblefax'] })).toBe('HumbleFax');
    expect(routeText('humblefax', { state: 'none', summary: null, reported_cost: [], routes: ['phaxio', 'signalwire'] }))
      .toBe('SignalWire (after Phaxio)');
    expect(routeText('phaxio', { state: 'none', summary: null, reported_cost: [], routes: ['direct'] })).toBe('Direct delivery');
  });

  it('falls back to the accepted provider while no attempt has a route yet', () => {
    expect(routeText('humblefax', undefined)).toBe('HumbleFax');
    expect(routeText('humblefax', { state: 'none', summary: null, reported_cost: [], routes: [] })).toBe('HumbleFax');
  });
});
