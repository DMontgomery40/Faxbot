// When to send (T13): the recipient's hours and learned busy hours on Recipients, Details; the send-by time
// on Send a fax; and the send-by sentence on Sent details.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { RecipientSchedule } from '../api/types';
import JobsList from '../components/JobsList';
import SendFax from '../components/SendFax';
import RecipientSchedulePanel from '../components/delivery/RecipientSchedule';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+12025550123';

const learned: RecipientSchedule = {
  number: NUMBER, time_zone: '', installation_time_zone: 'America/Denver', days: null, start: null, end: null,
  learn_busy: true, hours_sentence: 'Faxbot sends to this recipient at any time.',
  busy_hours: [{ label: 'Weekdays, 9:00 AM to 10:00 AM',
    sentence: 'Busy on 6 of the last 8 weekdays Faxbot called at this hour.' }],
  busy_sentence: 'Ordinary faxes wait until these hours end, unless their send-by time does not allow it. '
    + 'Urgent faxes always go at once.',
  failed_try: { route: 'sinch', label: 'Sinch', read_on: '2026-10-07', sources: ['https://sinch.com/voice/fax-api/'],
    sentence: 'Sinch publishes a price per page and does not say whether a failed try is charged.' },
  call_hours: [{ label: 'Weekdays, 9:00 AM to 10:00 AM',
    sentence: 'About 40 seconds a page on 10 delivered calls; 0 of 10 answered calls failed.' }],
  typical_hour: 'About 20 seconds a page on 40 delivered calls; 0 of 40 answered calls failed.',
  call_hours_sentence: 'An ordinary fax may wait up to 12 hours for an hour in which calls to this number take much '
    + 'less time a page or fail less often after the fax machine answers. Urgent faxes always go at once.',
};

describe('Recipients, Details: when to send', () => {
  it('shows the learned busy hours and what a failed try costs, and saves business hours', async () => {
    const writes: unknown[] = [];
    server.use(
      http.get('/routing/destinations/:number/schedule', () => HttpResponse.json(learned)),
      http.put('/routing/destinations/:number/schedule', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push(body);
        return HttpResponse.json({ ...learned, ...body, time_zone: body.time_zone ?? '',
          hours_sentence: 'This recipient takes faxes only Monday to Friday, 9:00 AM to 5:00 PM in their time zone '
            + '(America/New_York).' });
      }),
    );
    render(<RecipientSchedulePanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-schedule');
    expect(within(panel).getByTestId('schedule-hours').textContent).toBe('Faxbot sends to this recipient at any time.');
    expect(within(panel).getByTestId('schedule-busy-hours').textContent).toBe(
      'Weekdays, 9:00 AM to 10:00 AM: Busy on 6 of the last 8 weekdays Faxbot called at this hour.');
    expect(within(panel).getByTestId('schedule-failed-try').textContent).toBe(learned.failed_try.sentence);
    // Learned call hours (M26): each hour with enough calls, the typical hour, and what Faxbot does with them.
    expect(within(panel).getByTestId('schedule-call-hours').textContent).toBe(
      'Weekdays, 9:00 AM to 10:00 AM: About 40 seconds a page on 10 delivered calls; 0 of 10 answered calls failed.');
    expect(within(panel).getByTestId('schedule-typical-hour').textContent).toBe(
      'Any hour: About 20 seconds a page on 40 delivered calls; 0 of 40 answered calls failed.');
    expect(within(panel).getByTestId('schedule-call-hours-sentence').textContent).toBe(learned.call_hours_sentence);
    fireEvent.click(within(panel).getByLabelText('Takes faxes at any time'));
    expect(within(panel).getByTestId('schedule-days')).toBeTruthy();
    fireEvent.mouseDown(within(panel).getByLabelText("Recipient's time zone"));
    fireEvent.click(await screen.findByRole('option', { name: 'America/New_York' }));
    fireEvent.click(within(panel).getByRole('button', { name: 'Save when to send' }));
    expect(await screen.findByText('Saved for the next fax.')).toBeTruthy();
    expect(writes).toEqual([{ time_zone: 'America/New_York', days: ['mon', 'tue', 'wed', 'thu', 'fri'],
      start: '09:00', end: '17:00', learn_busy: true }]);
    expect(within(panel).getByTestId('schedule-hours').textContent).toContain('Monday to Friday, 9:00 AM to 5:00 PM');
  });

  it('turns busy-hour learning off and back to any time', async () => {
    const writes: unknown[] = [];
    server.use(
      http.get('/routing/destinations/:number/schedule', () => HttpResponse.json({ ...learned,
        days: ['mon', 'tue', 'wed', 'thu', 'fri'], start: '08:00', end: '18:00' })),
      http.put('/routing/destinations/:number/schedule', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push(body);
        return HttpResponse.json({ ...learned, ...body, time_zone: '' });
      }),
    );
    render(<RecipientSchedulePanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-schedule');
    fireEvent.click(within(panel).getByLabelText('Takes faxes at any time'));
    fireEvent.click(within(panel).getByRole('checkbox', { name: 'Learn the hours this number is usually busy, slow or failing' }));
    fireEvent.click(within(panel).getByRole('button', { name: 'Save when to send' }));
    await waitFor(() => expect(writes).toEqual([{ time_zone: null, days: null, start: null, end: null,
      learn_busy: false }]));
  });

  it('shows no save button to someone who may only read settings', async () => {
    server.use(http.get('/routing/destinations/:number/schedule', () => HttpResponse.json(learned)));
    render(<RecipientSchedulePanel client={client()} number={NUMBER} canWrite={false} />);
    const panel = await screen.findByTestId('recipient-schedule');
    expect(within(panel).queryByRole('button', { name: 'Save when to send' })).toBeNull();
  });
});

describe('Send a fax: send by', () => {
  it('sends the send-by time as an exact moment only when one is chosen', async () => {
    server.use(http.post('/fax', () => HttpResponse.json({ id: 'f'.repeat(32), status: 'queued', delivery_state: 'ready' },
      { status: 202 })));
    const appended = vi.spyOn(FormData.prototype, 'append');
    render(<SendFax client={client()} config={{ fax_disabled: false, max_file_size_mb: 10 }} configLoading={false}
      configError={null} />);
    fireEvent.change(screen.getByTestId('send-by'), { target: { value: '2026-10-08T17:00' } });
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: NUMBER } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement,
      { target: { files: [new File(['%PDF-1.4 note'], 'note.pdf', { type: 'application/pdf' })] } });
    fireEvent.click(screen.getByRole('button', { name: 'Send Fax' }));
    await waitFor(() => expect(appended.mock.calls.some(([name]) => name === 'send_by')).toBe(true));
    const sent = appended.mock.calls.find(([name]) => name === 'send_by')?.[1];
    expect(String(sent)).toBe(new Date('2026-10-08T17:00').toISOString());
    appended.mockClear();
    const file = new File(['%PDF-1.4'], 'page.pdf', { type: 'application/pdf' });
    await client().sendFax(NUMBER, file);
    expect(appended.mock.calls.some(([name]) => name === 'send_by')).toBe(false);
    appended.mockRestore();
  });
});

describe('Sent details: send by', () => {
  it('says the send-by time and warns when the fax may miss it', async () => {
    const JOB = 'd'.repeat(32);
    const job = { id: JOB, to_number: NUMBER, status: 'queued', backend: 'sinch', pages: 1,
      created_at: '2026-10-07T12:00:00', updated_at: '2026-10-07T12:00:00', delivery_state: 'ready',
      dispatch_mode: 'normal', delivery_version: 1 };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json({ ...job,
        waiting_reason: 'Waiting until 10:00 AM MDT: this number was busy at this hour on 6 of the last 8 weekdays '
          + 'Faxbot called it, and Sinch may charge for a failed try.',
        send_by: { at: '2026-10-07T17:00:00Z', at_risk: true,
          sentence: 'This fax may miss its send-by time of 11:00 AM MDT: it has not started yet.' } })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText(NUMBER));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect((await within(dialog).findByTestId('job-send-by')).textContent).toBe(
      'This fax may miss its send-by time of 11:00 AM MDT: it has not started yet.');
    expect(within(dialog).getByTestId('job-waiting-reason').textContent).toContain(
      'this number was busy at this hour on 6 of the last 8 weekdays');
  });
});
