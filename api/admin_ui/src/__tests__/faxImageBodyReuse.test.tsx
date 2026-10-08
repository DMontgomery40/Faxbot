// A fax image sent as only its new header lines, because the partner already held its pages: Sent says so from the
// server's sentence, and Costs → Savings counts the bytes apart from money.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList from '../components/JobsList';
import Savings from '../components/delivery/Savings';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const BODY_SENT = 'Delivered directly as a fax image to County Clinic, which already held these pages; only the new '
  + 'header lines went, 2.0 KB instead of 60 KB.';

describe('Costs → Savings: fax images sent as header lines', () => {
  it('counts the bytes as bytes, never as an estimate of money', async () => {
    const part = { saved: [], sentence: 'Nothing yet.', estimate: true };
    server.use(http.get('/routing/savings', () => HttpResponse.json({
      days: 30, since: '2026-09-08T00:00:00', estimate: true, sentence: 'Each figure is an estimate.',
      total_saved: [], total_sentence: 'No money saved in the last 30 days, as far as Faxbot can tell.',
      sending_together: part, direct_delivery: part,
      case_packets: { ...part, earlier_not_counted: false, counted_from: null, counted_from_sentence: null },
      direct_bytes: { bytes_saved: 59392, documents: 1, references: 0, patches: 0, fax_images: 1,
        sentence: '58 KB not sent in the last 30 days: 1 fax image sent as new header lines only, over pages the '
          + 'partner already held. These are bytes over the internet, not money; the calls were already saved.' },
    })));
    render(<Savings client={client()} />);
    const card = await screen.findByTestId('savings-bytes');
    expect(within(card).getByText(/1 fax image sent as new header lines only, over pages the partner already held/))
      .toBeTruthy();
    expect(within(card).queryByText('Estimate')).toBeNull();
    expect(screen.getByTestId('savings-total').textContent)
      .toBe('No money saved in the last 30 days, as far as Faxbot can tell.');
  });
});

describe('Sent: a fax image whose pages the partner held', () => {
  it("says only the new header lines went, from the server's sentence", async () => {
    const JOB = 'c'.repeat(32);
    const job = { id: JOB, to_number: '+15550100002', status: 'success', backend: 'phaxio', pages: 3,
      created_at: '2026-10-08T12:00:00', updated_at: '2026-10-08T12:01:00', delivery_state: 'success',
      dispatch_mode: 'normal', delivery_version: 3 };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
      http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({
        version: 3, state: 'success', dispatch_mode: 'normal', provider_id: 'phaxio', profile_id: 'p', revision_id: 'r',
        attempt: { id: 'att-2', phase: 'success', provider_sid: null, submitted_at: '2026-10-08T12:00:10',
          completed_at: '2026-10-08T12:00:20' },
        can_bind_provider_identity: false, bind_refusal_reason: null,
        events: [{ id: 'e1', attempt_id: 'att-2', kind: 'provider_observed', created_at: '2026-10-08T12:00:20',
          details: { status: 'success' } }], events_truncated: false })),
      http.get('/direct/deliveries', () => HttpResponse.json({ deliveries: [{
        message_id: 'att-2', direction: 'outbound', partner: 'County Clinic', fax_number: '+15550100002',
        kind: 'fax_image', state: 'accepted', status: 'Delivered directly as a fax image to County Clinic; no '
          + 'telephone call.', size_bytes: 61440, created_at: '2026-10-08T12:00:10',
        accepted_at: '2026-10-08T12:00:20', send_once: BODY_SENT }] })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+15550100002'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect(await within(dialog).findByText(BODY_SENT)).toBeTruthy();
    expect(within(dialog).queryByText(/faxed/i)).toBeNull();
  });
});
