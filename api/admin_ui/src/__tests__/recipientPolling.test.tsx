// Collecting faxes by polling (M21) on Recipients, Details: off until you turn it on, and only when you select
// Collect now.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { RecipientPolling } from '../api/types';
import RecipientPollingPanel from '../components/delivery/RecipientPolling';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+15555550199';
const NOTE = 'Collecting works only when the other fax server holds the fax for you to collect. Turn it on only for '
  + "your organization's own sites, and only after the other site has set its fax server to hold faxes for you.";

const off: RecipientPolling = {
  number: NUMBER, enabled: false, label: null, selective: null, note: NOTE, requests: [],
  advice: 'This number sent you 2 faxes (5 pages, about 3 minutes on the line) in the last 30 days, and the other '
    + 'site paid for those calls. Collecting them would cost your own phone line about $0.005.',
};

describe('Recipients, Details: collect faxes from this number', () => {
  it('shows the advice, turns collecting on, and collects only when asked', async () => {
    const writes: unknown[] = [];
    let collected = 0;
    server.use(
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
    await waitFor(() => expect(writes).toEqual([{ enabled: true, label: 'Denver office', selective: null }]));
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
    server.use(http.get('/routing/destinations/:number/polling', () => HttpResponse.json(off)));
    render(<RecipientPollingPanel client={client()} number={NUMBER} canWrite={false} />);
    const panel = await screen.findByTestId('recipient-polling');
    expect(within(panel).queryByRole('button', { name: 'Collect now' })).toBeNull();
  });
});
