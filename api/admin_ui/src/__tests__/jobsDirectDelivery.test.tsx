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
  const dialog = await screen.findByRole('dialog', { name: 'Job Details' });
  await within(dialog).findByText('Delivery Events');
  return dialog;
}

const ATTACH = 'Attach Confirmed Provider Fax ID';

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
    const dialog = await screen.findByRole('dialog', { name: 'Job Details' });
    expect(within(dialog).getByText(JOB)).toBeTruthy();
    expect(within(dialog).getByText('SIP trunk (Asterisk)')).toBeTruthy();
  });

  it('opens the fax Send just queued', async () => {
    jobServer('ready', delivery('ready', 'att-1', false, []), () => HttpResponse.json({ deliveries: [] }));
    const opened = vi.fn();
    render(<JobsList client={client()} openJobId={JOB} onOpened={opened} />);
    const dialog = await screen.findByRole('dialog', { name: 'Job Details' });
    expect(await within(dialog).findByText(JOB)).toBeTruthy();
    expect(opened).toHaveBeenCalledTimes(1);
  });
});
