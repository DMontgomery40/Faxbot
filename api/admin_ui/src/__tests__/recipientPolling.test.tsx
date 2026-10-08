// Collecting faxes by polling (M21) on Recipients, Details: off until you turn it on, and only when you select
// Collect now.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { RecipientHold, RecipientPolling } from '../api/types';
import RecipientPollingPanel from '../components/delivery/RecipientPolling';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+15555550199';
const NOTE = 'Collecting works only when the other fax server holds the fax for you to collect. Turn it on only for '
  + "your organization's own sites, and only after the other site has set its fax server to hold faxes for you.";

const off: RecipientPolling = {
  number: NUMBER, enabled: false, label: null, selective: null, note: NOTE, requests: [],
  has_password: false, collect_times: null, collect_days: null, time_zone: null, timetable: null,
  advice: 'This number sent you 2 faxes (5 pages, about 3 minutes on the line) in the last 30 days, and the other '
    + 'site paid for those calls. Collecting them would cost your own phone line about $0.005.',
};
const HOLD_NOTE = 'A held fax goes out only when this number calls Faxbot and asks for it, so that site pays for the '
  + "call. Turn it on only for your organization's own sites, and give them the selective polling address and "
  + 'password you set here.';
const holdOff: RecipientHold = { number: NUMBER, enabled: false, label: null, selective: null, has_password: false,
  note: HOLD_NOTE, held: [] };

describe('Recipients, Details: collect faxes from this number', () => {
  it('shows the advice, turns collecting on, and collects only when asked', async () => {
    const writes: unknown[] = [];
    let collected = 0;
    server.use(
      http.get('/routing/destinations/:number/polling/hold', () => HttpResponse.json(holdOff)),
      http.get('/routing/destinations/:number/polling', () => HttpResponse.json(off)),
      http.put('/routing/destinations/:number/polling', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push(body);
        return HttpResponse.json({ ...off, ...body });
      }),
      http.post('/routing/destinations/:number/polling/collect', () => {
        collected += 1;
        return HttpResponse.json({ ...off, enabled: true, label: 'Denver office', id: 'c'.repeat(32),
          sentence: 'Faxbot is calling the other fax server to collect the fax it holds for you.',
          requests: [{ id: 'c'.repeat(32), requested_at: '2026-10-08T15:00:00', requested: '8 Oct 9:00 AM MDT',
            requested_by: 'Ada', state: 'Calling', pages: null, inbound_fax_id: null,
            sentence: 'Faxbot is calling the other fax server to collect the fax it holds for you.' }] });
      }),
    );
    render(<RecipientPollingPanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-polling');
    expect(within(panel).getByTestId('polling-advice').textContent).toBe(off.advice);
    expect(within(panel).getByTestId('polling-note').textContent).toBe(NOTE);
    // Off: Collect now is not offered until collecting is turned on and saved.
    expect((within(panel).getByRole('button', { name: 'Collect now' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(panel).getByRole('checkbox', { name: 'Allow Faxbot to collect faxes this number holds for you' }));
    fireEvent.change(within(panel).getByLabelText('Other site'), { target: { value: 'Denver office' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Save collecting' }));
    // No password typed and no timetable touched: both stay as they are (null keeps).
    await waitFor(() => expect(writes).toEqual([{ enabled: true, label: 'Denver office', selective: null,
      password: null, collect_times: null, collect_days: null, time_zone: null }]));
    expect(collected).toBe(0);
    await waitFor(() => expect(
      (within(panel).getByRole('button', { name: 'Collect now' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(panel).getByRole('button', { name: 'Collect now' }));
    expect(await within(panel).findByText('Faxbot is calling the other fax server to collect the fax it holds for you.',
      { selector: '.MuiAlert-message' })).toBeTruthy();
    expect(collected).toBe(1);
    expect(within(panel).getByTestId('polling-requests').textContent).toBe(
      '8 Oct 9:00 AM MDT: Calling. Faxbot is calling the other fax server to collect the fax it holds for you.');
  });

  it('only reads for someone who cannot change settings', async () => {
    server.use(http.get('/routing/destinations/:number/polling', () => HttpResponse.json(off)),
      http.get('/routing/destinations/:number/polling/hold', () => HttpResponse.json(holdOff)));
    render(<RecipientPollingPanel client={client()} number={NUMBER} canWrite={false} />);
    const panel = await screen.findByTestId('recipient-polling');
    expect(within(panel).queryByRole('button', { name: 'Collect now' })).toBeNull();
    expect(within(await screen.findByTestId('recipient-hold')).queryByRole('button', { name: 'Hold a fax for it' })).toBeNull();
  });

  it('sets a password and a timetable without ever showing the password', async () => {
    const writes: Record<string, unknown>[] = [];
    const withPassword: RecipientPolling = { ...off, enabled: true, has_password: true, collect_times: '08:00,16:00',
      collect_days: 'mon,tue,wed,thu,fri', time_zone: 'America/Denver',
      timetable: 'Faxbot collects at 08:00 and 16:00 on weekdays (America/Denver).' };
    server.use(
      http.get('/routing/destinations/:number/polling', () => HttpResponse.json(off)),
      http.get('/routing/destinations/:number/polling/hold', () => HttpResponse.json(holdOff)),
      http.put('/routing/destinations/:number/polling', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json(withPassword);
      }),
    );
    render(<RecipientPollingPanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-polling');
    expect(within(panel).getByTestId('polling-timetable').textContent).toBe('Faxbot collects only when you select Collect now.');
    fireEvent.click(within(panel).getByRole('checkbox', { name: 'Allow Faxbot to collect faxes this number holds for you' }));
    fireEvent.change(within(panel).getByLabelText('Polling password'), { target: { value: '2468' } });
    fireEvent.change(within(panel).getByLabelText('Collect at'), { target: { value: '08:00,16:00' } });
    fireEvent.change(within(panel).getByLabelText('On days'), { target: { value: 'mon,tue,wed,thu,fri' } });
    fireEvent.change(within(panel).getByLabelText('Time zone'), { target: { value: 'America/Denver' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Save collecting' }));
    await waitFor(() => expect(writes).toEqual([{ enabled: true, label: null, selective: null, password: '2468',
      collect_times: '08:00,16:00', collect_days: 'mon,tue,wed,thu,fri', time_zone: 'America/Denver' }]));
    await waitFor(() => expect(within(panel).getByTestId('polling-timetable').textContent).toBe(withPassword.timetable));
    // Set: the field is empty again and says a password is set; the digits appear nowhere on the screen.
    expect((within(panel).getByLabelText('Polling password') as HTMLInputElement).value).toBe('');
    expect(panel.textContent).toContain('A password is set; leave empty to keep it');
    expect(panel.textContent).not.toContain('2468');
    // Removing it sends an empty password.
    fireEvent.click(within(panel).getByRole('checkbox', { name: 'Remove the password' }));
    fireEvent.click(within(panel).getByRole('button', { name: 'Save collecting' }));
    await waitFor(() => expect(writes.length).toBe(2));
    expect(writes[1].password).toBe('');
  });
});

describe('Recipients, Details: faxes this number collects from Faxbot', () => {
  it('turns holding on, holds a document, lists it, and withdraws it', async () => {
    const writes: Record<string, unknown>[] = [];
    let heldFiles = 0;
    let withdrawn = '';
    const on: RecipientHold = { ...holdOff, enabled: true, label: 'Denver office', selective: '77', has_password: true };
    const held = { id: 'a'.repeat(32), held_at: '2026-10-08T15:00:00', held: '8 Oct 9:00 AM MDT', held_by: 'Ada',
      pages: 2, name: 'notice.pdf', selective: '77', has_password: true, state: 'Held',
      sentence: 'Waiting for the other site to call and collect it.', gone: false };
    server.use(
      http.get('/routing/destinations/:number/polling', () => HttpResponse.json(off)),
      http.get('/routing/destinations/:number/polling/hold', () => HttpResponse.json(holdOff)),
      http.put('/routing/destinations/:number/polling/hold', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json(on);
      }),
      http.post('/routing/destinations/:number/polling/hold/faxes', () => {
        heldFiles += 1;
        return HttpResponse.json({ ...on, id: held.id, sentence: held.sentence, held: [held] });
      }),
      http.delete('/routing/destinations/:number/polling/hold/faxes/:id', ({ params }) => {
        withdrawn = String(params.id);
        return HttpResponse.json({ ...on, id: held.id, outcome: 'withdrawn',
          held: [{ ...held, state: 'Withdrawn', sentence: 'Withdrawn by Ada.', gone: true }] });
      }),
    );
    render(<RecipientPollingPanel client={client()} number={NUMBER} canWrite />);
    const section = await screen.findByTestId('recipient-hold');
    expect(within(section).getByTestId('hold-note').textContent).toBe(HOLD_NOTE);
    expect((within(section).getByRole('button', { name: 'Hold a fax for it' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(section).getByRole('checkbox', { name: 'Let this number call Faxbot and collect the faxes held for it' }));
    fireEvent.change(within(section).getByLabelText('Name of the other site'), { target: { value: 'Denver office' } });
    fireEvent.change(within(section).getByLabelText('Selective polling address it must give'), { target: { value: '77' } });
    fireEvent.change(within(section).getByLabelText('Polling password it must give'), { target: { value: '2468' } });
    fireEvent.click(within(section).getByRole('button', { name: 'Save holding' }));
    await waitFor(() => expect(writes).toEqual([{ enabled: true, label: 'Denver office', selective: '77', password: '2468' }]));
    expect(section.textContent).not.toContain('2468');
    await waitFor(() => expect(
      (within(section).getByRole('button', { name: 'Hold a fax for it' }) as HTMLButtonElement).disabled).toBe(false));
    const appended = vi.spyOn(FormData.prototype, 'append');
    const file = new File(['%PDF-1.4 notice'], 'notice.pdf', { type: 'application/pdf' });
    fireEvent.change(within(section).getByTestId('hold-file'), { target: { files: [file] } });
    await waitFor(() => expect(heldFiles).toBe(1));
    expect(appended.mock.calls.some(([name, value]) => name === 'file' && value === file)).toBe(true);
    appended.mockRestore();
    expect(await within(section).findByText(held.sentence, { selector: '.MuiAlert-message' })).toBeTruthy();
    expect(within(section).getByTestId('held-faxes').textContent).toContain(
      '8 Oct 9:00 AM MDT: notice.pdf, 2 pages. Held. Waiting for the other site to call and collect it.');
    fireEvent.click(within(section).getByRole('button', { name: 'Withdraw' }));
    await waitFor(() => expect(withdrawn).toBe(held.id));
    await waitFor(() => expect(within(section).getByTestId('held-faxes').textContent).toContain('Withdrawn. Withdrawn by Ada.'));
    expect(within(section).queryByRole('button', { name: 'Withdraw' })).toBeNull();
  });
});
