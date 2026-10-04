// A fax sent by direct delivery shows the partner's answer in Job Details
// instead of offering to attach a provider fax ID.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList from '../components/JobsList';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const JOB = 'a'.repeat(32);

const job = (state: string) => ({
  id: JOB, to_number: '+15550100001', status: state, backend: 'phaxio', pages: 1, created_at: '2026-10-03T12:00:00',
  updated_at: '2026-10-03T12:01:00', delivery_state: state, dispatch_mode: 'normal', delivery_version: 3,
});

const event = (id: string, attempt: string | null, kind: string, details: Record<string, string> = {}) => ({
  id, attempt_id: attempt, kind, created_at: '2026-10-03T12:00:30', details,
});

function delivery(state: string, attempt: string, canBind: boolean, events: ReturnType<typeof event>[]) {
  return {
    version: 3, state, dispatch_mode: 'normal', provider_id: 'phaxio', profile_id: 'profile', revision_id: 'revision',
    attempt: { id: attempt, phase: state === 'success' ? 'success' : 'uncertain', provider_sid: null,
      submitted_at: '2026-10-03T12:00:10', completed_at: state === 'success' ? '2026-10-03T12:00:20' : null },
    can_bind_provider_identity: canBind, bind_refusal_reason: canBind ? null : 'This fax already has a final result, so there is no fax ID to attach.',
    events, events_truncated: false,
  };
}

const record = (message_id: string, state: string) => ({
  message_id, direction: 'outbound', partner: 'Valley Hospital', fax_number: '+15550100001', state, status: '',
  size_bytes: 1000, created_at: '2026-10-03T12:00:10', accepted_at: state === 'accepted' ? '2026-10-03T12:00:20' : null,
});

function jobServer(state: string, detail: ReturnType<typeof delivery>, direct: Response | (() => Response)) {
  server.use(
    http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job(state)] })),
    http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job(state))),
    http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json(detail)),
    http.get('/direct/deliveries', typeof direct === 'function' ? direct : () => direct),
  );
}

async function openJob() {
  render(<JobsList client={client()} />);
  fireEvent.click(await screen.findByText('+15550100001'));
  const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
  await within(dialog).findByText('What happened');
  return dialog;
}

const ATTACH = 'Confirm receipt';

describe('Job Details for direct delivery', () => {
  it('waits for the partner instead of offering a provider fax ID', async () => {
    jobServer('reconciliation_required', delivery('reconciliation_required', 'att-1', true, [event('e1', 'att-1', 'submission_uncertain')]),
      () => HttpResponse.json({ deliveries: [record('att-1', 'uncertain'), record('other', 'accepted')] }));
    const dialog = await openJob();
    expect(await within(dialog).findByText("Waiting for the partner's answer.")).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: ATTACH })).toBeNull();
    expect(within(dialog).queryByText("A provider fax ID can't be added to this fax from here.")).toBeNull();
  });

  it('says the fax was delivered directly to the partner', async () => {
    jobServer('success', delivery('success', 'att-1', false, [event('e1', 'att-1', 'provider_observed', { status: 'success' })]),
      () => HttpResponse.json({ deliveries: [record('att-1', 'accepted')] }));
    const dialog = await openJob();
    expect(await within(dialog).findByText('Delivered directly to Valley Hospital.')).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: ATTACH })).toBeNull();
  });

  it('says a refused direct delivery went by fax, and still allows a provider fax ID for the fax', async () => {
    jobServer('reconciliation_required', delivery('reconciliation_required', 'att-2', true, [
      event('e1', 'att-1', 'submission_authorized'),
      event('e2', 'att-1', 'route_fallback', { category: 'partner_not_received' }),
      event('e3', 'att-2', 'route_assigned', { route: 'sinch' }),
    ]), () => HttpResponse.json({ deliveries: [record('att-1', 'refused')] }));
    const dialog = await openJob();
    expect(await within(dialog).findByText('Direct delivery refused, sent by fax instead.')).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: ATTACH })).toBeTruthy();
    expect(within(dialog).getByText('Trying the next route')).toBeTruthy();
    expect(within(dialog).getByText('The direct delivery partner did not receive it')).toBeTruthy();
    expect(within(dialog).getByText('Route: Sinch')).toBeTruthy();
  });

  it('works as before when direct delivery records are not available to this account', async () => {
    jobServer('reconciliation_required', delivery('reconciliation_required', 'att-1', true, [event('e1', 'att-1', 'submission_uncertain')]),
      () => HttpResponse.json({ detail: 'This operation is not permitted.' }, { status: 403 }));
    const dialog = await openJob();
    expect(within(dialog).getByRole('button', { name: ATTACH })).toBeTruthy();
    expect(within(dialog).queryByText(/partner|directly/)).toBeNull();
  });
});

describe('Jobs list wording', () => {
  it('names the provider in words and keeps the job ID for the detail view only', async () => {
    const sip = { ...job('success'), backend: 'sip' };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [sip] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(sip)),
      http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json(delivery('success', 'att-1', false, []))),
      http.get('/direct/deliveries', () => HttpResponse.json({ deliveries: [] })),
    );
    render(<JobsList client={client()} />);
    const table = (await screen.findByText('+15550100001')).closest('table') as HTMLElement;
    expect(within(table).getByText('SIP trunk (Asterisk)')).toBeTruthy();
    expect(within(table).queryByText(/^sip$|Backend|Job ID|[0-9a-f]{8}\.\.\./)).toBeNull();
    fireEvent.click(within(table).getByText('+15550100001'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect(within(dialog).getByText(JOB)).toBeTruthy();
    expect(within(dialog).getByText('SIP trunk (Asterisk)')).toBeTruthy();
  });

  it('shows the whole error sentence, wrapped between words', async () => {
    const sentence = 'The call connected but no fax data came back from the carrier.';
    server.use(http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [{ ...job('failed'), error: sentence }] })));
    render(<JobsList client={client()} />);
    const error = await screen.findByTestId('job-error');
    expect(error.textContent).toBe(sentence);
    expect(error.getAttribute('title')).toBeNull();
    const style = window.getComputedStyle(error);
    expect(style.whiteSpace).not.toBe('nowrap');
    expect(style.textOverflow).not.toBe('ellipsis');
  });

  it('names the original provider in words and shows no internal account or sign-in IDs', async () => {
    const detail = { ...delivery('reconciliation_required', 'att-1', true, [
      event('e1', 'att-1', 'provider_identity_bound', { actor: 'principal:bd26f4bd-011a-4e14-9d1f-052975afafa4', provider_sid: 'FAX-123' }),
    ]), provider_id: 'sip', profile_id: 'bd26f4bd-011a-4e14-9d1f-052975afafa4' };
    jobServer('reconciliation_required', detail, () => HttpResponse.json({ deliveries: [] }));
    const dialog = await openJob();
    expect(within(dialog).getByText('Original provider').closest('li')?.textContent).toContain('SIP trunk (Asterisk)');
    expect(within(dialog).queryByText(/Original Provider Account|bd26f4bd|principal:|Operator:/)).toBeNull();
    expect(within(dialog).getByText(/Provider fax ID: FAX-123/)).toBeTruthy();
  });

  it('opens the fax Send just queued', async () => {
    jobServer('ready', delivery('ready', 'att-1', false, []), () => HttpResponse.json({ deliveries: [] }));
    const opened = vi.fn();
    render(<JobsList client={client()} openJobId={JOB} onOpened={opened} />);
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect(await within(dialog).findByText(JOB)).toBeTruthy();
    expect(opened).toHaveBeenCalledTimes(1);
  });
});

describe('Sent', () => {
  const OTHER = 'b'.repeat(32);

  it('lists each fax with its route and cost, saying "estimate" until the carrier reports', async () => {
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 2, jobs: [
        { ...job('success'), backend: 'sip' }, { ...job('success'), id: OTHER, to_number: '+15550100002' },
      ] })),
      http.get('/routing/fax-costs', ({ request }) => {
        expect(new URL(request.url).searchParams.get('ids')).toBe(`${JOB},${OTHER}`);
        return HttpResponse.json({ costs: {
          [JOB]: { state: 'reported', summary: 'Telnyx charged $0.005 for this call.', reported_cost: [{ currency: 'USD', amount: '0.005' }], estimated_cost: [] },
          [OTHER]: { state: 'waiting', summary: 'Cost not reported yet.', reported_cost: [], estimated_cost: [{ currency: 'USD', amount: '0.07' }] },
        } });
      }),
    );
    const send = vi.fn();
    render(<JobsList client={client()} onSendFax={send} />);
    expect(await screen.findByRole('heading', { name: 'Sent' })).toBeTruthy();
    for (const name of ['To', 'Status', 'Route', 'Pages', 'Cost', 'Created', 'Updated']) {
      expect(screen.getByRole('columnheader', { name })).toBeTruthy();
    }
    const charged = (await screen.findByText('$0.005')).closest('tr') as HTMLElement;
    expect(within(charged).getByText('SIP trunk (Asterisk)')).toBeTruthy();
    expect(within(charged).getByText('$0.005').getAttribute('title')).toBe('Telnyx charged $0.005 for this call.');
    const estimated = (await screen.findByText('$0.07 estimate')).closest('tr') as HTMLElement;
    expect(within(estimated).getByText('+15550100002')).toBeTruthy();
    expect(screen.getByText('2 faxes')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Send a fax' }));
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('shows no Send a fax button to people who may not send', async () => {
    render(<JobsList client={client()} />);
    expect(await screen.findByText('No faxes sent yet.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Send a fax' })).toBeNull();
  });

  it('explains the delivery attempts and confirms receipt with the provider fax ID, which never sends the fax again', async () => {
    const posted: unknown[] = [];
    const sends: unknown[] = [];
    jobServer('reconciliation_required', delivery('reconciliation_required', 'att-1', true, []), () => HttpResponse.json({ deliveries: [] }));
    server.use(
      http.post(`/admin/fax-jobs/${JOB}/reconcile`, async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json(delivery('reconciliation_required', 'att-1', false, [
          event('e1', 'att-1', 'operator_identity_bound', { provider_sid: 'FAX-9' })]));
      }),
      http.post('/fax', () => { sends.push(true); return HttpResponse.json({}); }),
    );
    const dialog = await openJob();
    expect(within(dialog).getByText('Each try to send this fax and what the provider reported, as Faxbot recorded it.')).toBeTruthy();
    expect(within(dialog).getByText(/This never sends the fax again\./)).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Provider fax ID'), { target: { value: 'FAX-9' } });
    fireEvent.click(within(dialog).getByRole('checkbox'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm receipt' }));
    expect(await within(dialog).findByText('Receipt confirmed. Select Refresh status to check delivery with the provider.')).toBeTruthy();
    expect(posted).toEqual([{ expected_version: 3, provider_sid: 'FAX-9', confirm_original_account: true }]);
    expect(sends).toEqual([]);
  });
});
