// Room for fax calls: the trunk page's limits, calls at once to a recipient, Urgent on Send,
// the waiting sentence in Sent and "Waiting for a free line" on the Overview.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { Destination } from '../api/deliveryTypes';
import type { SipTrunkSettings } from '../api/sipTypes';
import Dashboard from '../components/Dashboard';
import SendFax from '../components/SendFax';
import FaxSettings, { callsAtOnceHint, callsPerSecondHint } from '../components/FaxSettings';
import JobsList, { URGENT_TEXT } from '../components/JobsList';
import Destinations, { CALLS_AT_ONCE_HELP } from '../components/delivery/Destinations';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+12025550123';

const telnyx = {
  calls_per_second: 5, calls_at_once: 2,
  note: 'Telnyx allows 20 new calls a second but charges extra above 5 a second, and allows 2 calls at once until the account is verified to Level 2 (then 10).',
  sources: ['https://support.telnyx.com/en/articles/7834487-calls-per-second-cps-surcharge',
    'https://developers.telnyx.com/docs/voice/sip-trunking/configuration/concurrent-limits'],
  read_on: '2026-10-06',
};

describe('the trunk page', () => {
  it('sets calls at once and new calls per second, each with one sentence and the carrier limits with sources', () => {
    const form = { fax_lines: 2, max_calls: 0, calls_per_second: 0, carrier_limits: telnyx } as unknown as SipTrunkSettings;
    const update = vi.fn();
    render(<FaxSettings form={form} update={update} />);
    const box = screen.getByTestId('trunk-capacity');
    fireEvent.change(within(box).getByLabelText('Calls at once'), { target: { value: '3' } });
    fireEvent.change(within(box).getByLabelText('New calls per second'), { target: { value: '1' } });
    expect(update).toHaveBeenCalledWith('max_calls', 3);
    expect(update).toHaveBeenCalledWith('calls_per_second', 1);
    expect(callsAtOnceHint(form)).toBe('The most faxes Faxbot sends or receives at the same time on your phone line. '
      + 'Others wait their turn. Leave 0 to use the number of fax lines (2).');
    expect(callsPerSecondHint(form)).toBe('The most fax calls Faxbot starts in one second on your phone line. Others wait '
      + 'a moment. Leave 0 to use 5, the most your carrier takes at no extra cost.');
    expect(callsPerSecondHint({ ...form, carrier_limits: null })).toBe('The most fax calls Faxbot starts in one second on '
      + 'your phone line. Others wait a moment. Leave 0 for no limit.');
    const limits = screen.getByTestId('carrier-limits');
    expect(limits.textContent).toContain(telnyx.note);
    const links = [...limits.querySelectorAll('a')];
    expect(links.map((link) => [link.textContent, link.getAttribute('href')])).toEqual([
      ['support.telnyx.com', telnyx.sources[0]], ['developers.telnyx.com', telnyx.sources[1]]]);
  });
});

describe('a recipient', () => {
  it('saves calls at once to this number with the existing setting', async () => {
    const patched: unknown[] = [];
    const destination: Destination = { number: NUMBER, display_name: null, notes: null, preferred_route: null,
      accepts_references: false, version: 2, routes: [], estimated_cost_30_days: [], max_calls: null };
    server.use(
      http.get('/routing/destinations/:number', () => HttpResponse.json({ ...destination, direct_partner: null,
        recommended_routes: [], available_routes: [] })),
      http.patch('/routing/destinations/:number', async ({ request }) => {
        patched.push(await request.json());
        return HttpResponse.json(destination);
      }),
    );
    render(<Destinations client={client()} destinations={[destination]} canWrite onChanged={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: `Details for ${NUMBER}` }));
    const dialog = await screen.findByRole('dialog');
    const choice = await within(dialog).findByLabelText('Calls at once to this number') as HTMLSelectElement;
    expect(choice.value).toBe('');
    expect(within(dialog).getByText(CALLS_AT_ONCE_HELP)).toBeTruthy();
    fireEvent.change(choice, { target: { value: '0' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(patched[0]).toMatchObject({ max_calls: 0, version: 2 });
  });
});

describe('Sent and the Overview', () => {
  it('says why a fax waits and that it is urgent', async () => {
    const JOB = 'e'.repeat(32);
    const job = { id: JOB, to_number: NUMBER, status: 'queued', backend: 'sip', pages: 1, created_at: '2026-10-06T12:00:00',
      updated_at: '2026-10-06T12:00:00', delivery_state: 'ready', dispatch_mode: 'normal', delivery_version: 1 };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json({ ...job, urgent: true,
        waiting_reason: 'Waiting: another fax is calling this number.' })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText(NUMBER));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect((await within(dialog).findByTestId('job-waiting-reason')).textContent).toBe(
      'Waiting: another fax is calling this number.');
    expect(within(dialog).getByTestId('job-urgent').textContent).toBe(URGENT_TEXT);
  });

  it('counts the faxes waiting for a free line', async () => {
    server.use(http.get('/admin/health-status', () => HttpResponse.json({ timestamp: '2026-10-06T12:00:00Z',
      backend: 'sip', backend_healthy: true, jobs: { queued: 3, in_progress: 1, recent_failures: 0, waiting_for_line: 2 },
      inbound_enabled: true, api_keys_configured: true, require_auth: true })));
    render(<Dashboard client={client()} />);
    expect((await screen.findByTestId('waiting-for-line')).textContent).toBe('Waiting for a free line:2');
  });
});

describe('Send', () => {
  it('sends an urgent fax only when Urgent is checked', async () => {
    server.use(http.post('/fax', () => HttpResponse.json({ id: 'f'.repeat(32), status: 'queued' }, { status: 202 })));
    const appended = vi.spyOn(FormData.prototype, 'append');
    const file = new File(['%PDF-1.4'], 'page.pdf', { type: 'application/pdf' });
    await client().sendFax(NUMBER, file, { urgent: true });
    expect(appended.mock.calls.filter(([name]) => name === 'urgent').map(([, value]) => String(value))).toEqual(['true']);
    appended.mockClear();
    await client().sendFax(NUMBER, file);
    expect(appended.mock.calls.some(([name]) => name === 'urgent')).toBe(false);
    appended.mockRestore();
  });
});

describe('the Send screen', () => {
  it('has an Urgent checkbox that marks the fax urgent', async () => {
    server.use(http.post('/fax', () => HttpResponse.json({ id: 'f'.repeat(32), status: 'queued', delivery_state: 'ready' },
      { status: 202 })));
    const appended = vi.spyOn(FormData.prototype, 'append');
    render(<SendFax client={client()} config={{ fax_disabled: false, max_file_size_mb: 10 }} configLoading={false}
      configError={null} />);
    const urgent = screen.getByTestId('send-urgent').querySelector('input') as HTMLInputElement;
    expect(urgent.checked).toBe(false);
    fireEvent.click(urgent);
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: NUMBER } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement,
      { target: { files: [new File(['%PDF-1.4 note'], 'note.pdf', { type: 'application/pdf' })] } });
    fireEvent.click(screen.getByRole('button', { name: 'Send Fax' }));
    await waitFor(() => expect(appended.mock.calls.some(([name, value]) => name === 'urgent' && String(value) === 'true'))
      .toBe(true));
    appended.mockRestore();
  });
});
