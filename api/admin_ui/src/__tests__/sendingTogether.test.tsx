// Sending short faxes to one number together: the per-number setting in a fax number's
// Details, a waiting fax in Jobs with "Send now", and the Send page's "Send now" choice.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList from '../components/JobsList';
import SendFax from '../components/SendFax';
import { SendingTogetherPanel, formatWaitTime, togetherLine } from '../components/delivery/SendingTogether';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+12025550123';
const JOB = 'c'.repeat(32);

const off = {
  number: NUMBER, enabled: false, max_wait_minutes: 10, max_pages: 30, mixed_senders: false, version: 0,
  saves_money: true,
  route_sentence: 'Your Telnyx SIP trunk bills each call with a 1-minute minimum, so faxes sent together cost less.',
  state_sentence: 'Off: faxes to this number go straight away.', agreement: null, history: [],
  savings: { calls: 0, faxes: 0, calls_saved: 0, estimated_saving: [], is_estimate: true,
    sentence: 'No faxes to this number have been sent together in the last 30 days.' },
  agreement_text: 'This recipient has agreed to receive several documents in one call.',
};

describe('Sending together in a fax number\'s Details', () => {
  it('turns on only after the recipient\'s agreement is recorded, and sends it with the settings', async () => {
    const saved: unknown[] = [];
    server.use(
      http.get('/batching/numbers/:number', () => HttpResponse.json(off)),
      http.put('/batching/numbers/:number', async ({ request }) => {
        saved.push(await request.json());
        return HttpResponse.json({
          ...off, enabled: true, version: 1, max_wait_minutes: 5,
          state_sentence: 'On: a fax to this number waits up to 5 minutes to go in one call with others.',
          agreement: { action: 'on', by: 'Owner', at: '2026-10-03T22:30:00+00:00', recipient_agreed: true,
            max_wait_minutes: 5, max_pages: 30, mixed_senders: false },
          savings: { ...off.savings, calls: 2, faxes: 5, calls_saved: 3, estimated_saving: [{ currency: 'USD', amount: '0.015' }],
            sentence: 'Last 30 days: 5 faxes in 2 calls, 3 calls saved, about $0.015 saved (estimate).' },
        });
      }),
    );
    render(<SendingTogetherPanel client={client()} number={NUMBER} canWrite />);
    expect(await screen.findByText(off.route_sentence)).toBeTruthy();
    expect(screen.getByText('Off: faxes to this number go straight away.')).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Send short faxes to this number together in one call' }));
    const save = screen.getByRole('button', { name: 'Save sending together' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(screen.getByRole('checkbox', { name: off.agreement_text }));
    fireEvent.change(screen.getByLabelText('Longest wait (minutes)'), { target: { value: '5' } });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    expect(await screen.findByText('Last 30 days: 5 faxes in 2 calls, 3 calls saved, about $0.015 saved (estimate).')).toBeTruthy();
    expect(saved).toEqual([{ enabled: true, recipient_agreed: true, max_wait_minutes: 5, max_pages: 30,
      mixed_senders: false, version: 0 }]);
    expect(screen.getByText(/The recipient's agreement was recorded by Owner on/)).toBeTruthy();
  });

  it('shows a person without settings access the setting without changing it', async () => {
    server.use(http.get('/batching/numbers/:number', () => HttpResponse.json(off)));
    render(<SendingTogetherPanel client={client()} number={NUMBER} canWrite={false} />);
    expect(await screen.findByText(off.state_sentence)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save sending together' })).toBeNull();
  });
});

describe('A waiting fax in Jobs', () => {
  const later = new Date(Date.now() + 8 * 60_000).toISOString();
  const job = (together: object | null) => ({
    id: JOB, to_number: '*******0123', status: 'queued', backend: 'sip', pages: 1, created_at: '2026-10-03T22:30:00',
    updated_at: '2026-10-03T22:30:00', delivery_state: 'ready', dispatch_mode: 'normal', delivery_version: 1, together,
  });

  it('says until when it waits and sends it now on request', async () => {
    const waiting = { state: 'waiting', reference: 'Faxbot cccccccc', waiting_until: later, send_now: false };
    let released = false;
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job(waiting)] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job(waiting))),
      http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({ version: 1, state: 'ready', dispatch_mode: 'normal',
        provider_id: 'sip', profile_id: 'p', revision_id: 'r', attempt: null, can_bind_provider_identity: false,
        bind_refusal_reason: null, events: [], events_truncated: false })),
      http.get(`/batching/faxes/${JOB}`, () => HttpResponse.json({ ...waiting, sentence: 'Waiting to go with other faxes to this number.', share: null })),
      http.post(`/batching/faxes/${JOB}/send-now`, () => {
        released = true;
        return HttpResponse.json({ ...waiting, send_now: true, sentence: 'Waiting to go with other faxes to this number.', share: null });
      }),
    );
    render(<JobsList client={client()} />);
    const line = await screen.findByTestId('job-together');
    expect(line.textContent).toBe(`Waiting to go with other faxes to this number until ${formatWaitTime(later)}.`);
    fireEvent.click(screen.getByText('*******0123'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Send now' }));
    await waitFor(() => expect(released).toBe(true));
    expect(await within(dialog).findByText('Going now with the faxes waiting for this number.')).toBeTruthy();
  });

  it('says a fax went in one call with others, its separator reference and its share of the charge', async () => {
    const together = { state: 'together', reference: 'Faxbot cccccccc', documents: 3, document_number: 2, others: 2 };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [{ ...job(together), status: 'success', delivery_state: 'success' }] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json({ ...job(together), status: 'success', delivery_state: 'success' })),
      http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({ version: 4, state: 'success', dispatch_mode: 'normal',
        provider_id: 'sip', profile_id: 'p', revision_id: 'r', attempt: null, can_bind_provider_identity: false,
        bind_refusal_reason: null, events: [{ id: 'e1', attempt_id: 'a1', kind: 'sent_together', created_at: '2026-10-03T22:40:00', details: {} }],
        events_truncated: false })),
      http.get(`/batching/faxes/${JOB}`, () => HttpResponse.json({ ...together, sentence: 'Sent in one call with 2 other faxes.',
        share: { amount: '0.002', call_amount: '0.005', currency: 'USD', basis: 'reported',
          sentence: "Its share of the call's charge, split by pages: $0.002 of $0.005." } })),
    );
    render(<JobsList client={client()} />);
    expect((await screen.findByTestId('job-together')).textContent).toBe('Sent in one call with 2 other faxes.');
    fireEvent.click(screen.getByText('*******0123'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect(await within(dialog).findByText("Its share of the call's charge, split by pages: $0.002 of $0.005.")).toBeTruthy();
    expect(within(dialog).getByText('Its separator page says Faxbot cccccccc (document 2 of 3).')).toBeTruthy();
    expect(within(dialog).getByText('Going in one call with other faxes to this number')).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: 'Send now' })).toBeNull();
  });

  it('has nothing to say about a fax that never waited', () => {
    expect(togetherLine(null)).toBeNull();
    expect(togetherLine({ state: 'separate', reference: 'Faxbot cccccccc' })).toBeNull();
  });
});

describe('Send offers "Send now" only for a number that sends together', () => {
  beforeEach(() => window.sessionStorage.clear());

  it('shows the choice for that number and sends it with the fax', async () => {
    server.use(
      http.get('/batching/check', ({ request }) => {
        const to = new URL(request.url).searchParams.get('to');
        return HttpResponse.json(to === NUMBER
          ? { number: NUMBER, sends_together: true, wait_minutes: 10,
            sentence: 'Faxes to this number wait up to 10 minutes to go in one call with others.' }
          : { number: to, sends_together: false, wait_minutes: null, sentence: null });
      }),
      http.post('/fax', () => HttpResponse.json({ id: 'b'.repeat(32), status: 'queued', delivery_state: 'ready' }, { status: 202 })),
    );
    const api = client();
    const sendFax = vi.spyOn(api, 'sendFax');
    render(<SendFax client={api} config={{ fax_disabled: false, max_file_size_mb: 10 } as never} configLoading={false} configError={null} />);
    const number = screen.getByRole('textbox', { name: /Destination Number/ });
    fireEvent.change(number, { target: { value: '+12025550199' } });
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(screen.queryByTestId('send-now')).toBeNull();
    fireEvent.change(number, { target: { value: NUMBER } });
    expect(await screen.findByText('Faxes to this number wait up to 10 minutes to go in one call with others.', {}, { timeout: 2000 })).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: /Send now/ }));
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement,
      { target: { files: [new File(['%PDF-1.4 note'], 'note.pdf', { type: 'application/pdf' })] } });
    fireEvent.click(screen.getByRole('button', { name: 'Send Fax' }));
    await waitFor(() => expect(sendFax).toHaveBeenCalledTimes(1));
    expect(sendFax.mock.calls[0][2]).toMatchObject({ sendNow: true });
  });
});
