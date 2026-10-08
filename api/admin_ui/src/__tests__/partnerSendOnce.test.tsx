// Partners → Send once: the receiving side offers its intake for named numbers, the sender accepts, either ends it.
// Savings shows the bytes reuse and changes saved apart from money, and Sent says a fax went once to an intake.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { SendOnceAgreement } from '../api/deliveryTypes';
import DeliveryRoutes from '../components/DeliveryRoutes';
import JobsList from '../components/JobsList';
import Savings from '../components/delivery/Savings';
import { parseNumbers } from '../components/delivery/PartnerSendOnce';
import { server } from '../test/server';

type Recorded = { method: string; path: string; body: unknown };

function recorder() {
  const calls: Recorded[] = [];
  const record = async (request: Request) => {
    const text = await request.text();
    calls.push({ method: request.method, path: new URL(request.url).pathname, body: text ? JSON.parse(text) : null });
  };
  return { calls, record };
}

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const partner = {
  id: 'peer-1', organization: 'County Clinic', fax_number: '+15550100002', endpoint: 'https://clinic.example',
  state: 'verified', status: 'Verified; faxes to this number are delivered directly.', code_sent: false,
  code_expires_at: null, verified_at: '2026-10-07T01:00:00', expires_at: null, version: 3,
};

const BYTES = { bytes_saved: 0, documents: 0, references: 0, patches: 0,
  sentence: 'No documents went to partners as references or changes in the last 30 days.' };

const offered: SendOnceAgreement = {
  id: 'a'.repeat(32), peer_id: 'peer-1', partner: 'County Clinic', role: 'sender', state: 'offered',
  numbers: ['+15550100012', '+15550100013'], intake: 'Central records',
  summary: 'County Clinic\'s intake "Central records" files your faxes for 2 numbers.',
  status: 'County Clinic offers to file your faxes to these numbers at its intake. Accept only if these numbers are theirs.',
  told: true, created_at: '2026-10-08T01:00:00', updated_at: '2026-10-08T01:00:00',
};

function partnersHandlers(record: (request: Request) => Promise<void>, agreements: SendOnceAgreement[] = []) {
  server.use(
    http.get('/direct/peers', () => HttpResponse.json({ peers: [partner] })),
    http.get('/direct/send-once', () => HttpResponse.json({ agreements, bytes: BYTES })),
    http.post('/direct/peers/:id/send-once', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...offered, role: 'receiver', status: 'Offered; waiting for County Clinic to accept.',
        placements: [{ fax_number: '+15550100012', mailbox: 'Billing', held: null }],
        detail: 'Offered. The partner accepts it in their Faxbot.' });
    }),
    http.post('/direct/send-once/:id/accept', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...offered, state: 'active',
        detail: "Accepted. Faxes to these numbers now go to the partner's intake." });
    }),
    http.post('/direct/send-once/:id/withdraw', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...offered, state: 'withdrawn',
        detail: 'Ended. Faxes to these numbers go by your usual routes again.' });
    }),
  );
}

describe('Partners → Send once', () => {
  it('offers your intake for the numbers you name', async () => {
    const { calls, record } = recorder();
    partnersHandlers(record);
    render(<DeliveryRoutes client={client()} canWrite section="partners" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Send once' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('No send-once agreement with County Clinic yet.')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Offer your intake to County Clinic' }));
    fireEvent.change(within(dialog).getByLabelText('Your fax numbers'), { target: { value: '+15550100012, +15550100013' } });
    fireEvent.change(within(dialog).getByLabelText('Intake name'), { target: { value: 'Central records' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Offer' }));
    expect(await within(dialog).findByText('Offered. The partner accepts it in their Faxbot.')).toBeTruthy();
    expect(calls).toEqual([{ method: 'POST', path: '/direct/peers/peer-1/send-once',
      body: { numbers: ['+15550100012', '+15550100013'], intake: 'Central records' } }]);
  });

  it("accepts a partner's offer after showing its numbers, and ends it", async () => {
    const { calls, record } = recorder();
    partnersHandlers(record, [offered]);
    render(<DeliveryRoutes client={client()} canWrite section="partners" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Send once' }));
    const dialog = await screen.findByRole('dialog');
    const card = await within(dialog).findByTestId('send-once-agreement');
    expect(within(card).getByText(offered.status)).toBeTruthy();
    expect(within(card).getByText('+15550100012')).toBeTruthy();
    fireEvent.click(within(card).getByRole('button', { name: 'Accept' }));
    expect(await within(dialog).findByText("Accepted. Faxes to these numbers now go to the partner's intake.")).toBeTruthy();
    fireEvent.click(within(card).getByRole('button', { name: 'End agreement' }));
    expect(await within(dialog).findByText('Ended. Faxes to these numbers go by your usual routes again.')).toBeTruthy();
    expect(calls.map((call) => call.path)).toEqual([`/direct/send-once/${offered.id}/accept`,
      `/direct/send-once/${offered.id}/withdraw`]);
  });

  it('reads numbers separated by spaces, commas or semicolons', () => {
    expect(parseNumbers(' +15550100012,+15550100013; +15550100014 ')).toEqual(
      ['+15550100012', '+15550100013', '+15550100014']);
  });
});

describe('Costs → Savings bytes', () => {
  it('shows the bytes reuse and changes saved as bytes, never as an estimate of money', async () => {
    const part = { saved: [], sentence: 'Nothing yet.', estimate: true };
    server.use(http.get('/routing/savings', () => HttpResponse.json({
      days: 30, since: '2026-09-08T00:00:00', estimate: true, sentence: 'Each figure is an estimate.',
      total_saved: [], total_sentence: 'No money saved in the last 30 days, as far as Faxbot can tell.',
      sending_together: part, direct_delivery: part,
      case_packets: { ...part, earlier_not_counted: false, counted_from: null, counted_from_sentence: null },
      direct_bytes: { bytes_saved: 2048, documents: 2, references: 2, patches: 0,
        sentence: '2.0 KB not sent in the last 30 days: 2 as a reference to a copy the partner held. These are bytes '
          + 'over the internet, not money; the calls were already saved.' },
    })));
    render(<Savings client={client()} />);
    const card = await screen.findByTestId('savings-bytes');
    expect(within(card).getByText('Bytes saved by reuse and patches')).toBeTruthy();
    expect(within(card).getByText(/2.0 KB not sent in the last 30 days/)).toBeTruthy();
    expect(within(card).queryByText('Estimate')).toBeNull();
    expect(screen.getByTestId('savings-total').textContent).toBe('No money saved in the last 30 days, as far as Faxbot can tell.');
  });
});

describe('Sent: a fax sent once to a partner intake', () => {
  it("says it went once to the partner's intake, from the server's sentence", async () => {
    const JOB = 'b'.repeat(32);
    const job = { id: JOB, to_number: '+15550100012', status: 'success', backend: 'phaxio', pages: 1,
      created_at: '2026-10-08T12:00:00', updated_at: '2026-10-08T12:01:00', delivery_state: 'success',
      dispatch_mode: 'normal', delivery_version: 3 };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
      http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({
        version: 3, state: 'success', dispatch_mode: 'normal', provider_id: 'phaxio', profile_id: 'p', revision_id: 'r',
        attempt: { id: 'att-1', phase: 'success', provider_sid: null, submitted_at: '2026-10-08T12:00:10',
          completed_at: '2026-10-08T12:00:20' },
        can_bind_provider_identity: false, bind_refusal_reason: null,
        events: [{ id: 'e1', attempt_id: 'att-1', kind: 'provider_observed', created_at: '2026-10-08T12:00:20',
          details: { status: 'success' } }], events_truncated: false })),
      http.get('/direct/deliveries', () => HttpResponse.json({ deliveries: [{
        message_id: 'att-1', direction: 'outbound', partner: 'County Clinic', fax_number: '+15550100012',
        state: 'accepted', status: 'Accepted by the recipient.', size_bytes: 1000, created_at: '2026-10-08T12:00:10',
        accepted_at: '2026-10-08T12:00:20',
        send_once: "Sent once to County Clinic's intake, which delivers to 3 recipients." }] })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+15550100012'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect(await within(dialog).findByText("Sent once to County Clinic's intake, which delivers to 3 recipients.")).toBeTruthy();
  });
});
