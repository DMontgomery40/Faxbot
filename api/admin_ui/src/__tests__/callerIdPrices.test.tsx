// Costs → Prices & plans → Prices by caller ID: the decks, each sending account's caller ID and its confirmation,
// a number's prices, and importing a deck. Synthetic numbers and prices only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import CallerIdPrices from '../components/delivery/CallerIdPrices';
import type { CallerIdPrices as Prices, SendingCaller } from '../api/countriesTypes';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const TRUNK: SendingCaller = {
  account: 'sip', label: 'Telnyx', provider: 'sip', provider_label: 'Your own trunk', site: null,
  caller_id: '+13035550100', priced_by_caller_id: true, sentence: 'Calls from this account present +13035550100.',
  eligibility: null,
};
const PHAXIO: SendingCaller = {
  account: 'phaxio', label: 'Phaxio', provider: 'phaxio', provider_label: 'Phaxio', site: null, caller_id: null,
  priced_by_caller_id: false, sentence: 'Phaxio sets its own sending number, so its calls are not priced by caller ID.',
  eligibility: null,
};
const PRICES: Prices = {
  decks: [{ route: 'sip-telnyx', format: 'twilio', source_url: 'https://example.com/deck.csv', published_on: '2026-10-09',
    imported_at: '2026-10-09T12:00:00', imported_by: 'Synthetic Admin', currency: 'USD', rows: 15,
    by_type: { eea: 4, non_surcharged: 6, surcharged: 5 } }],
  callers: [TRUNK, PHAXIO],
  layouts: { faxbot: 'One row per price.', telnyx: 'Telnyx publishes its rate deck only in Mission Control.' },
};

describe('Prices by caller ID', () => {
  it('lists the decks and each account, and confirms a caller ID with its evidence', async () => {
    const sent: unknown[] = [];
    server.use(
      http.get('/routing/caller-id-prices', () => HttpResponse.json(PRICES)),
      http.post('/routing/caller-ids/confirm', async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json({ callers: [{ ...TRUNK, eligibility: { account: 'sip', caller_id: TRUNK.caller_id,
          state: 'confirmed', bought_here: true, evidence: 'Number order 1', evidence_url: null, recorded_by: 'A',
          recorded_at: '2026-10-09T12:00:00' } }, PHAXIO] });
      }),
    );
    render(<CallerIdPrices client={client()} routes={['sip-telnyx']} canWrite />);
    const section = await screen.findByTestId('caller-id-prices');
    expect(section.textContent).toContain('sip-telnyx: 15 prices (4 for EEA caller IDs, 6 for listed caller IDs, 5 for any other caller ID)');
    expect(section.textContent).toContain('Not confirmed');
    expect(section.textContent).toContain('Phaxio sets its own sending number');
    expect(screen.queryByRole('button', { name: 'Confirm undefined on Phaxio' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm +13035550100 on Telnyx' }));
    const dialog = await screen.findByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Confirm' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true); // evidence is required
    fireEvent.change(within(dialog).getByLabelText(/How you know/), { target: { value: 'Number order 1' } });
    fireEvent.click(within(dialog).getByLabelText('This number was bought on this account'));
    fireEvent.click(confirm);
    await waitFor(() => expect(screen.getByText('Confirmed, bought on this account')).toBeTruthy());
    expect(sent).toEqual([{ account: 'sip', caller_id: '+13035550100', evidence: 'Number order 1', evidence_url: null,
      bought_here: true }]);
    expect(screen.getByRole('button', { name: 'Withdraw +13035550100 on Telnyx' })).toBeTruthy();
  });

  it("shows a number's prices on each account, and says when no deck covers it", async () => {
    server.use(
      http.get('/routing/caller-id-prices', () => HttpResponse.json(PRICES)),
      http.get('/routing/caller-id-prices/quote', ({ request }) => {
        const to = new URL(request.url).searchParams.get('to');
        return HttpResponse.json(to === '+43123456789' ? { number: to, quotes: [{ account: 'sip', label: 'Telnyx',
          eligibility: 'unconfirmed', caller_id: '+13035550100', row: null, cheaper: null,
          sentence: 'Priced at the rate for any other caller ID ($0.155 a minute).' }] } : { number: to, quotes: [] });
      }),
    );
    render(<CallerIdPrices client={client()} routes={[]} canWrite={false} />);
    await screen.findByTestId('caller-id-prices');
    expect(screen.queryByRole('button', { name: 'Import a rate deck' })).toBeNull();
    fireEvent.change(screen.getByLabelText('Fax number'), { target: { value: '+43123456789' } });
    fireEvent.click(screen.getByRole('button', { name: 'Show prices' }));
    expect((await screen.findByTestId('caller-id-quotes')).textContent)
      .toBe('Telnyx: Priced at the rate for any other caller ID ($0.155 a minute).');
    fireEvent.change(screen.getByLabelText('Fax number'), { target: { value: '+33123456789' } });
    fireEvent.click(screen.getByRole('button', { name: 'Show prices' }));
    await waitFor(() => expect(screen.getByTestId('caller-id-quotes').textContent)
      .toBe('No rate deck priced by caller ID covers +33123456789.'));
  });

  it('imports a deck file and says what it took from the rate card', async () => {
    let form: FormData | null = null;
    server.use(
      http.get('/routing/caller-id-prices', () => HttpResponse.json({ ...PRICES, decks: [] })),
      http.post('/routing/rate-cards/:route/caller-id-prices', async ({ request, params }) => {
        form = await request.formData();
        return HttpResponse.json({ deck: { ...PRICES.decks[0], route: params.route }, skipped: [], skipped_count: 0,
          terms: 'Rows without their own billing step use 60-second steps, in USD, from your rate card.' });
      }),
    );
    render(<CallerIdPrices client={client()} routes={['sip-telnyx']} canWrite />);
    expect((await screen.findByTestId('caller-id-prices')).textContent).toContain('No rate deck priced by caller ID yet.');
    fireEvent.click(screen.getByRole('button', { name: 'Import a rate deck' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('Mission Control');
    const file = new File(['ISO,Country,Description,Price / min,Origination Prefixes,Destination Prefixes\n'], 'deck.csv',
      { type: 'text/csv' });
    fireEvent.change(within(dialog).getByLabelText('Rate deck file'), { target: { files: [file] } });
    fireEvent.change(within(dialog).getByLabelText('Published or read on'), { target: { value: '2026-10-09' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Imported 15 prices for sip-telnyx.');
    expect(form!.get('published_on')).toBe('2026-10-09');
    expect((form!.get('file') as File).name).toBe('deck.csv');
  });

  it('says when the prices could not be loaded, and stays quiet without permission', async () => {
    server.use(http.get('/routing/caller-id-prices', () => HttpResponse.json({ detail: 'x' }, { status: 500 })));
    const failed = render(<CallerIdPrices client={client()} routes={[]} canWrite />);
    expect((await screen.findByTestId('caller-id-prices-unread')).textContent)
      .toBe('Prices by caller ID could not be loaded. Try again.');
    failed.unmount();
    server.use(http.get('/routing/caller-id-prices', () => HttpResponse.json({ detail: 'x' }, { status: 403 })));
    const denied = render(<CallerIdPrices client={client()} routes={[]} canWrite />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(denied.container.textContent).toBe('');
  });
});
