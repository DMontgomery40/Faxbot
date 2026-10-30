import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { FaxJob } from '../api/types';
import JobsList, { CANT_TELL_LABEL, deliveryNotice, jobStatusLabel, problemText, routeText } from '../components/JobsList';
import { server } from '../test/server';

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

// D26: a fax refused while held. The server keeps the stored "Refused by <name>: <reason>" and sends what Sent shows:
// what happened first, a person by name, a key only as "an API key" in the list and by its name in the details.
describe('Sent: a fax refused while held', () => {
  const reason = 'Telnyx account disabled for a negative balance; add funds in the Telnyx portal under Billing, '
    + 'then send this referral again from Send a fax';
  const refused = uncertain({
    status: 'failed', delivery_state: 'failed', recipient: 'Faxbeep US test line',
    error: `Not sent: ${reason}. Decided through an API key.`,
    refusal: { sentence: `Not sent: ${reason}.`, decided_by: 'Decided through an API key.',
      decided_by_detail: 'Decided through the installation key.' } });

  it('leads with what happened and names a key only in the details', () => {
    expect(problemText(refused)).toBe(`Not sent: ${reason}. Decided through an API key.`);
    expect(problemText(refused, true)).toBe(`Not sent: ${reason}. Decided through the installation key.`);
    const person = { ...refused, refusal: { sentence: 'Not sent: wrong recipient.', decided_by: 'Decided by Dana Ruiz.',
      decided_by_detail: 'Decided by Dana Ruiz.' } };
    expect(problemText(person)).toBe('Not sent: wrong recipient. Decided by Dana Ruiz.');
    // Any other problem reads as the server sent it; a settled fax says it in its notice instead.
    expect(problemText({ ...refused, refusal: null, error: 'The line was busy.' })).toBe('The line was busy.');
    expect(problemText({ ...refused, refusal: null, settled_cant_tell: true })).toBeNull();
  });

  it('shows the whole sentence in the Status cell, the recipient above the number, and the key in the details', async () => {
    const id = refused.id;
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [refused] })),
      http.get(`/admin/fax-jobs/${id}`, () => HttpResponse.json(refused)),
      http.get(`/admin/fax-jobs/${id}/delivery`, () => HttpResponse.json({
        version: 2, state: 'failed', dispatch_mode: 'normal', provider_id: 'humblefax', profile_id: 'profile',
        revision_id: 'revision', attempt: null, can_bind_provider_identity: false, bind_refusal_reason: null,
        events: [{ id: 'e1', attempt_id: null, kind: 'recipient_named', created_at: '2026-10-10T20:45:00', details: {} }],
        events_truncated: false })),
      http.get('/direct/deliveries', () => HttpResponse.json({ deliveries: [] })),
      // The rest of the details: nothing to add for this fax.
      http.get(`/court/faxes/${id}`, () => HttpResponse.json({ court: null })),
      http.get(`/direct/admission/faxes/${id}`, () => HttpResponse.json({ sentences: [], waiting: false })),
      http.get(`/header-notice/faxes/${id}`, () => HttpResponse.json({ notice: null, sentence: null })),
      http.get(`/intake/sources/faxes/${id}`, () => HttpResponse.json({ detail: 'This fax did not come from a connector.' },
        { status: 404 })),
      http.get(`/routing/after-answer/faxes/${id}`, () => HttpResponse.json({ sentences: [] })),
      http.get(`/routing/stations/faxes/${id}`, () => HttpResponse.json({ sentences: [] })),
    );
    render(<JobsList client={new AdminAPIClient({ kind: 'key', key: 'synthetic-key' })} />);
    const cell = await screen.findByTestId('job-error');
    // Never cut: the cell holds every word, and the browser wraps it between words.
    expect(cell.textContent).toBe(`Not sent: ${reason}. Decided through an API key.`);
    expect(cell.textContent).not.toContain('installation key');
    expect((await screen.findByTestId('job-recipient')).textContent).toBe('Faxbeep US test line');
    fireEvent.click(screen.getByText('Faxbeep US test line'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect((await within(dialog).findByTestId('job-detail-problem')).textContent)
      .toBe(`Not sent: ${reason}. Decided through the installation key.`);
    expect(within(dialog).getByText('Faxbeep US test line, +15550100002')).toBeTruthy();
    expect(await within(dialog).findByText("Recipient's name given with the fax")).toBeTruthy();
  });
});
