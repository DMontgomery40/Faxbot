// Measured fax codings: the Sent detail says which coding went and why, and Send a fax says when 9 in 10 such
// calls finish. The server words every sentence; the console shows them as they come.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList from '../components/JobsList';
import SendFax from '../components/SendFax';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
const JOB = 'c'.repeat(32);
const SENT = 'Sent with MH: 20% shorter than MMR for these pages.';
const MEASURED = 'Measured on these pages at 14,400 bit/s: MH about 49 seconds, MR about 59 seconds, '
  + 'MMR about 1 minute 1 second.';

function job(coding: Record<string, unknown> | null) {
  return { id: JOB, to_number: '+15550100002', status: 'success', backend: 'sip', pages: 1,
    created_at: '2026-10-08T12:00:00', updated_at: '2026-10-08T12:01:00', delivery_state: 'success',
    dispatch_mode: 'normal', delivery_version: 3, coding };
}

function serve(found: ReturnType<typeof job>) {
  server.use(
    // Not from a connector (the Sent detail asks who requested each fax).
    http.get('/intake/sources/faxes/:id', () => HttpResponse.json({ detail: 'This fax did not come from a connector.' },
      { status: 404 })),
    http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [found] })),
    http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(found)),
    http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({
      version: 3, state: 'success', dispatch_mode: 'normal', provider_id: 'sip', profile_id: 'profile',
      revision_id: 'revision', attempt: { id: 'att-1', phase: 'success', provider_sid: null,
        submitted_at: '2026-10-08T12:00:10', completed_at: '2026-10-08T12:00:50' },
      can_bind_provider_identity: false, bind_refusal_reason: 'This fax already has a final result.', events: [],
      events_truncated: false })),
  );
}

describe('Which fax coding a sent fax went with', () => {
  it('shows the coding and what was measured in the fax details', async () => {
    serve(job({ requested: 'MH', negotiated: 'MH', measured: true, compared: 'MMR', pages: 1,
      bits: { MH: 698656, MR: 848360, MMR: 873336 }, receiver_known: true, sentence: SENT,
      measured_sentence: MEASURED }));
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+15550100002'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    const line = await within(dialog).findByTestId('job-coding');
    expect(line.textContent).toBe(`Fax coding${SENT}${MEASURED}`);
  });

  it('shows no coding line for a fax a fax service sent', async () => {
    serve(job(null));
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+15550100002'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    await within(dialog).findByText('Pages');
    expect(within(dialog).queryByTestId('job-coding')).toBeNull();
  });
});

describe('When such a call finishes', () => {
  it('says when 9 in 10 such calls finish, under how the cost was worked out', async () => {
    const finish = '9 in 10 such calls should finish within about 1 minute 6 seconds, from an assumed spread of '
      + '15% either way until this number has 3 faxes of its own.';
    const basis = 'Billed as 1 minute at $0.005 a minute, or more about 25% of the time, so about $0.00625 is '
      + 'expected; about 59 seconds on the line, for typical pages at a typical fax speed.';
    server.use(http.get('/routing/destinations/:number', ({ params }) => HttpResponse.json({ number: params.number,
      display_name: null, notes: null, preferred_route: null, accepts_references: false, version: 0, routes: [],
      estimated_cost_30_days: [], direct_partner: null, available_routes: [],
      recommended_routes: [{ route: 'sip', label: 'Telnyx', reason: 'cheapest', explanation: 'Cheapest.',
        estimated_cost_one_page: { currency: 'USD', amount: '0.005' }, pages: 1,
        estimated_cost: { currency: 'USD', amount: '0.005' }, rate: '$0.005 a minute, at least 1 minute',
        included_in_plan: false, monthly_fee: null }] })),
    http.get('/routing/predict', () => HttpResponse.json({ to: '+12025550123', number_class: 'local',
      number_class_text: 'a local number', pages: 1, layout: 'normal', resolution: 'fine', sentence: '', note: '',
      routes: [{ route: 'sip', label: 'Telnyx', billed_pages: 0, seconds: 59, billed_seconds: 60,
        seconds_to_next_step: 1, expected_billed_seconds: 75, p90_seconds: 66.2, finish_sentence: finish,
        cost: { currency: 'USD', amount: '0.00625' }, cost_text: '$0.00625', marginal: false,
        headline: 'About $0.00625 for this 1-page fax.', basis }] })));
    render(<SendFax client={client()} config={{ fax_disabled: false, max_file_size_mb: 10 }} configLoading={false}
      configError={null} />);
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    const page = new File(['%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Type /Pages /Count 1 >> endobj\n'],
      'one.pdf', { type: 'application/pdf' });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [page] } });
    await waitFor(() => expect(screen.getByTestId('send-cost-basis').textContent).toBe(basis), { timeout: 2000 });
    expect(screen.getByTestId('send-cost-finish').textContent).toBe(finish);
  });
});
