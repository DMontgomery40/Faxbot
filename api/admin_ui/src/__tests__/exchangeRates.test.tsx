// Prices & plans → Exchange rates: the ECB reference rates (on by default), your own dated rates, and what is used.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import ExchangeRates, { EXCHANGE_INTRO, type ExchangeView } from '../components/delivery/ExchangeRates';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const base: ExchangeView = {
  reference: { enabled: true, sentence: 'The ECB reference rates of 9 October 2026 for 3 currencies against the euro.',
    date: '2026-10-09', date_text: '9 October 2026', current: true,
    source_url: 'https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html',
    read_on: '2026-10-10', rates: [{ currency: 'GBP', per_euro: '0.84' }, { currency: 'JPY', per_euro: '177' },
      { currency: 'USD', per_euro: '1.12' }] },
  own: [], reference_max_age_days: 7, own_max_age_days: 35,
};

const withOwn: ExchangeView = { ...base, own: [{ base: 'GBP', quote: 'USD', rate: '1.27', date: '2026-09-30',
  date_text: '30 September 2026', current: true, note: 'September invoice',
  sentence: '1 GBP = 1.27 USD, your rate of 30 September 2026. It is used instead of the ECB rate for this pair.' }] };

describe('Exchange rates on Prices & plans', () => {
  it('shows the reference rates with their date, and sets and stops using your own rate', async () => {
    const sent: unknown[] = [];
    server.use(
      http.get('/routing/exchange-rates', () => HttpResponse.json(base)),
      http.put('/routing/exchange-rates/own', async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(withOwn);
      }),
      http.post('/routing/exchange-rates/own/clear', async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(base);
      }),
    );
    render(<ExchangeRates client={client()} canWrite />);
    const panel = await screen.findByTestId('exchange-rates');
    expect(within(panel).getByText(EXCHANGE_INTRO)).toBeTruthy();
    expect(within(panel).getByTestId('exchange-reference-sentence').textContent).toBe(base.reference.sentence);
    expect(within(panel).getByTestId('exchange-reference-rates').textContent).toBe('GBP 0.84 · JPY 177 · USD 1.12');
    expect((within(panel).getByRole('checkbox', { name: 'Read the ECB reference rates' }) as HTMLInputElement).checked).toBe(true);
    fireEvent.click(within(panel).getByRole('button', { name: 'Set your own rate' }));
    const dialog = await screen.findByRole('dialog', { name: 'Set your own exchange rate' });
    fireEvent.change(within(dialog).getByLabelText('One unit of (currency code)'), { target: { value: 'gbp' } });
    fireEvent.change(within(dialog).getByLabelText('Is worth'), { target: { value: '1.27' } });
    fireEvent.change(within(dialog).getByLabelText('Of (currency code)'), { target: { value: 'usd' } });
    fireEvent.change(within(dialog).getByLabelText("The rate's date"), { target: { value: '2026-09-30' } });
    fireEvent.change(within(dialog).getByLabelText('Where it comes from'), { target: { value: 'September invoice' } });
    expect(within(dialog).getByTestId('exchange-preview').textContent).toBe('1 GBP = 1.27 USD');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    const row = await within(panel).findByRole('row', { name: /1 GBP = 1.27 USD/ });
    expect(row.textContent).toContain('30 September 2026');
    expect(row.textContent).toContain('Yes');
    expect(sent[0]).toEqual({ base: 'GBP', quote: 'USD', rate: '1.27', rate_date: '2026-09-30', note: 'September invoice' });
    fireEvent.click(within(panel).getByRole('button', { name: 'Stop using your GBP to USD rate' }));
    await waitFor(() => expect(within(panel).getByText('You have not set any rates of your own.')).toBeTruthy());
    expect(sent[1]).toEqual({ base: 'GBP', quote: 'USD' });
  });

  it('turns the reference rates off and says why a read failed', async () => {
    const switched: unknown[] = [];
    server.use(
      http.get('/routing/exchange-rates', () => HttpResponse.json(base)),
      http.put('/routing/exchange-rates/reference', async ({ request }) => {
        switched.push(await request.json());
        return HttpResponse.json({ ...base, reference: { ...base.reference, enabled: false,
          sentence: 'Faxbot does not read the ECB reference rates; the last ones it read, of 9 October 2026, are used until they are more than 7 days old.' } });
      }),
      http.post('/routing/exchange-rates/refresh', () => HttpResponse.json({ ...base,
        refreshed: { outcome: 'failed', error: 'Faxbot could not reach the ECB from this server.', date: null } })),
    );
    render(<ExchangeRates client={client()} canWrite />);
    const panel = await screen.findByTestId('exchange-rates');
    fireEvent.click(within(panel).getByRole('button', { name: 'Read them now' }));
    expect((await within(panel).findByTestId('exchange-refresh-message')).textContent).toBe(
      'Faxbot could not read the ECB reference rates: Faxbot could not reach the ECB from this server.');
    fireEvent.click(within(panel).getByRole('checkbox', { name: 'Read the ECB reference rates' }));
    await waitFor(() => expect(switched).toEqual([{ enabled: false }]));
    expect((await within(panel).findByTestId('exchange-reference-sentence')).textContent).toContain(
      'Faxbot does not read the ECB reference rates');
    expect(within(panel).queryByRole('button', { name: 'Read them now' })).toBeNull();
  });

  it('shows the rates without changing them for someone who may only read', async () => {
    server.use(http.get('/routing/exchange-rates', () => HttpResponse.json(withOwn)));
    render(<ExchangeRates client={client()} canWrite={false} />);
    const panel = await screen.findByTestId('exchange-rates');
    await within(panel).findByRole('row', { name: /1 GBP = 1.27 USD/ });
    expect((within(panel).getByRole('checkbox', { name: 'Read the ECB reference rates' }) as HTMLInputElement).disabled).toBe(true);
    expect(within(panel).queryByRole('button', { name: 'Set your own rate' })).toBeNull();
    expect(within(panel).queryByRole('button', { name: /Stop using/ })).toBeNull();
  });

  it('says so when the rates cannot be loaded', async () => {
    server.use(http.get('/routing/exchange-rates', () => HttpResponse.json({ detail: 'Delivery route storage is unavailable.' }, { status: 503 })));
    render(<ExchangeRates client={client()} canWrite />);
    expect((await screen.findByRole('alert')).textContent).toBeTruthy();
    expect(screen.queryByTestId('exchange-rates')).toBeNull();
  });
});
