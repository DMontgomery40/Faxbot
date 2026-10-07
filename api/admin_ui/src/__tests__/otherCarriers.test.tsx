import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { CarrierComparison, CarrierPrice } from '../api/deliveryTypes';
import OtherCarriers, { differenceText } from '../components/delivery/OtherCarriers';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const usd = (amount: string) => [{ currency: 'USD', amount }];

function carrier(id: string, name: string, total: string, changes: Partial<CarrierPrice> = {}): CarrierPrice {
  return {
    id, name, kind: 'trunk', yours: false, total: usd(total), complete: true, sending: usd(total), receiving: usd('0'),
    monthly: usd('0'), not_priced: 0, numbers_not_priced: 0, over_budget: false, cheapest: false, difference: [],
    source_url: 'https://example.invalid/prices', advertised_on: '2026-10-07',
    sentence: `About ${total} for your 2 sent faxes and 1 received fax (estimate).`, ...changes,
  };
}

// Synthetic: Telnyx (yours) cost about $1.02; AnveoDirect would have cost about $0.16; Sinch prices no numbers.
function comparison(): CarrierComparison {
  return {
    days: 30, estimate: true, advice_only: true, sent: 2, received: 1, cheapest: 'sip-anveo',
    sentence: "At published prices, AnveoDirect trunk would have cost least for your last 30 days of faxing: about $0.16, "
      + "$0.86 less than your current services' $1.02 (estimate).",
    switching_sentence: 'Changing carriers means moving (porting) your fax numbers to the new carrier and opening an '
      + 'account there, often under a contract; Faxbot only compares published prices and never switches anything.',
    unpublished_sentence: 'Gamma trunk and eFax publish no price Faxbot can use, so they are left out.',
    current: { total: usd('1.0164'), complete: true, not_priced: 0, routes: ['Telnyx trunk'] },
    carriers: [
      carrier('sip-anveo', 'AnveoDirect trunk', '0.157', { cheapest: true, difference: usd('0.8594') }),
      carrier('sip-telnyx', 'Telnyx trunk', '1.0164', { yours: true, difference: usd('0') }),
      carrier('humblefax', 'HumbleFax', '10.00', { kind: 'plan', difference: usd('-8.9836') }),
      carrier('sinch', 'Sinch', '0.405', { kind: 'service', complete: false, numbers_not_priced: 1 }),
    ],
  };
}

describe('Costs → Recommendations → Other carriers', () => {
  it('says there is nothing to compare before any fax, and that Faxbot never switches', async () => {
    render(<OtherCarriers client={client()} />);
    expect((await screen.findByTestId('carriers-sentence')).textContent)
      .toBe('You sent and received no faxes in the last 30 days, so there is nothing to compare yet.');
    expect(screen.getByTestId('carriers-switching').textContent).toContain('never switches anything');
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('lists each carrier with its estimate, what it leaves out, and the difference from now', async () => {
    server.use(http.get('/routing/recommendations/carriers', () => HttpResponse.json(comparison())));
    let counted: number | null = -1;
    render(<OtherCarriers client={client()} onCount={(count) => { counted = count; }} />);
    expect((await screen.findByTestId('carriers-sentence')).textContent).toContain('AnveoDirect trunk would have cost least');
    const rows = screen.getAllByTestId('carrier-row');
    expect(rows).toHaveLength(4);
    const cells = (row: HTMLElement) => within(row).getAllByRole('cell').map((cell) => cell.textContent);
    expect(within(rows[0]).getByText('Cheapest')).toBeTruthy();
    expect(cells(rows[0]).slice(1, 4)).toEqual(['$0.16', '-', '$0.86 less']);
    expect(within(rows[1]).getByText('Yours')).toBeTruthy();
    expect(cells(rows[1])[3]).toBe('Same');
    expect(cells(rows[2])[3]).toBe('$8.98 more');
    // Sinch publishes no number price: left out, and no difference is claimed.
    expect(cells(rows[3]).slice(2, 4)).toEqual(['1 number', '-']);
    expect(screen.getByTestId('carriers-unpublished').textContent).toContain('Gamma trunk and eFax');
    expect(screen.getByText('Advice only')).toBeTruthy();
    expect(counted).toBe(1);
    expect(document.body.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}|\btrue\b|\bfalse\b|sip-anveo/);
  });

  it('reads a difference in plain words', () => {
    expect(differenceText([])).toBe('-');
    expect(differenceText(usd('0.00'))).toBe('Same');
    expect(differenceText(usd('2.50'))).toBe('$2.50 less');
    expect(differenceText(usd('-0.05'))).toBe('$0.05 more');
  });
});
