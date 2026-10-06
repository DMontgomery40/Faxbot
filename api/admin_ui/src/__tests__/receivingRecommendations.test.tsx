import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import ReceivingRecommendations, { readOnText, windowText } from '../components/delivery/ReceivingRecommendations';
import { newReceivingAdvice, server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const usd = (amount: string) => [{ currency: 'USD', amount }];
const costs = (today: string, lines: string, still: string, total: string, pooled: string) => ({
  billed_by_the_minute: usd(today), channels: usd(lines), still_billed_by_the_minute: usd(still),
  number_rental: usd('3.00'), total_today: usd(total), total_with_pool: usd(pooled), difference: usd('0.00'),
});

// Synthetic: two busy local numbers that would share 1 line, a toll-free number that stays billed by the minute.
function sharedAdvice() {
  const advice = newReceivingAdvice();
  return {
    ...advice,
    sentence: 'Put 2 of your Telnyx numbers on one shared line: in the last 30 days that would have cost about $15.40 '
      + 'instead of $31.80, and no caller would have heard busy (estimate).',
    history: { enough: true, first_call_at: '2026-07-01T09:00:00', days: 96 },
    pool: {
      state: 'share', sentence: 'Put 2 of your Telnyx numbers on one shared line: in the last 30 days that would have '
        + 'cost about $15.40 instead of $31.80, and no caller would have heard busy (estimate).',
      note: null, pool_numbers: ['+13035550100', '+13035550101'], channels: 1, calls: 900, turned_away: 0, peak: 1,
      needed: 1, busy_windows: [], busy_windows_total: 0,
      numbers: [
        { number: '+13035550100', kind: 'local', eligible: true, reason: null, in_pool: true, calls_before: 450,
          calls: 450, billed_by_the_minute: usd('14.40') },
        { number: '+13035550101', kind: 'local', eligible: true, reason: null, in_pool: true, calls_before: 450,
          calls: 450, billed_by_the_minute: usd('14.40') },
        { number: '+18005550199', kind: 'toll_free', eligible: false, reason: 'Toll-free numbers stay billed by the minute.',
          in_pool: false, calls_before: 0, calls: 0, billed_by_the_minute: [] },
      ],
      check: costs('28.80', '12.00', '0.40', '31.80', '15.40'),
      choose: { ...costs('28.80', '12.00', '0.40', '31.80', '15.40'), calls: 900, peak: 1, turned_away: 0 },
      break_even: 'One shared line at $12.00 a month costs as much as 3,750 received minutes at $0.0032 a minute.',
      assumptions: ['Telnyx calls a shared line an inbound channel: it takes one call at a time, with no charge per minute.',
        'Faxbot only recommends; it never changes your Telnyx account.'],
    },
    quiet_numbers: {
      state: 'quiet', monthly_total: usd('1.00'),
      sentence: 'One of your Telnyx numbers had 2 calls or fewer in the last 30 days; it costs about $1.00 a month to '
        + 'keep (estimate). Check that no one still faxes a number before you give it up.',
      numbers: [{ number: '+18005550199', received: 0, sent: 0, monthly_rental: usd('1.00') }],
    },
    connections: {
      sentence: 'Your 2 fax services cost $10.00 a month in fixed fees (estimate). Keeping only Telnyx would cost $0.00 '
        + 'a month, $10.00 less, if it can carry all your numbers and calls; keep a second service if you need a backup.',
      items: [{ name: 'Telnyx', kind: 'trunk', monthly_fee: usd('0.00') },
        { name: 'HumbleFax', kind: 'provider', monthly_fee: usd('10.00') }],
    },
  };
}

describe('Costs → Recommendations → Receiving', () => {
  it('shows one sentence while there is too little call history, and the fax services on their own', async () => {
    render(<ReceivingRecommendations client={client()} />);
    const lines = await screen.findByTestId('receiving-lines');
    expect(lines.textContent).toBe('Faxbot needs 60 days of call history to advise on shared lines; it has none yet.');
    expect(screen.queryByTestId('receiving-quiet')).toBeNull();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.getByTestId('receiving-services').textContent)
      .toContain('Telnyx is your only fax service, so there is no second monthly fee to save.');
    expect(screen.getByText('Published prices used')).toBeTruthy();
  });

  it('shows shared-line advice with its window, every figure marked an estimate, and money as money', async () => {
    server.use(http.get('/routing/recommendations/receiving', () => HttpResponse.json(sharedAdvice())));
    render(<ReceivingRecommendations client={client()} />);
    const lines = await screen.findByTestId('receiving-lines');
    expect(within(lines).getByTestId('receiving-sentence').textContent).toContain('Put 2 of your Telnyx numbers on one shared line');
    // The heading's chip and the comparison table's first column both say it.
    expect(within(lines).getAllByText('Estimate')).toHaveLength(2);
    const advice = sharedAdvice();
    expect(within(lines).getByTestId('receiving-window').textContent).toBe(
      `Figures from calls between ${windowText(advice.windows.check)}; the advice was chosen from calls between `
      + `${windowText(advice.windows.choose)}.`);
    const numbers = within(lines).getByRole('table', { name: 'Your numbers' });
    expect(within(numbers).getByText('Billed by the minute, last 30 days (estimate)')).toBeTruthy();
    expect(within(numbers).getAllByText('Shared lines')).toHaveLength(2);
    expect(within(numbers).getByText('Toll-free numbers stay billed by the minute.')).toBeTruthy();
    expect(within(numbers).getAllByText('$14.40')).toHaveLength(2);
    const compared = within(lines).getByRole('table', { name: 'Shared lines compared with billing by the minute' });
    const total = within(compared).getByText('Total with shared lines').closest('tr') as HTMLElement;
    expect(within(total).getAllByText('$15.40')).toHaveLength(2);
    expect(within(lines).getByText('Most calls at once in the last 30 days: 1.')).toBeTruthy();
    expect(within(lines).getByText('What these estimates assume')).toBeTruthy();
    const quiet = screen.getByTestId('receiving-quiet');
    expect(quiet.textContent).toContain('it costs about $1.00 a month to keep (estimate)');
    expect(within(quiet).getByRole('table', { name: 'Numbers with few calls' })).toBeTruthy();
    const services = within(screen.getByTestId('receiving-services')).getByRole('table', { name: 'Fax services' });
    expect(within(services).getByText('$10.00')).toBeTruthy();
    // No raw dates, identifiers or true/false on the screen.
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/\d{4}-\d{2}-\d{2}T|\btrue\b|\bfalse\b|_/);
  });

  it('names the busy stretch in local time when the chosen lines would have turned a caller away', async () => {
    const advice = sharedAdvice();
    Object.assign(advice.pool, {
      state: 'turned_away', turned_away: 1, needed: 2,
      sentence: "Don't switch yet: with one shared line for 2 of your Telnyx numbers, 1 caller would have heard a busy "
        + 'signal in the last 30 days (estimate).',
      busy_windows: [{ start: '2026-09-15T19:00:00', end: '2026-09-15T19:04:00', turned_away: 1, numbers: ['+13035550101'] }],
    });
    server.use(http.get('/routing/recommendations/receiving', () => HttpResponse.json(advice)));
    render(<ReceivingRecommendations client={client()} />);
    const busy = await screen.findByTestId('receiving-busy');
    const start = new Date('2026-09-15T19:00:00Z').toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    expect(busy.textContent).toContain(`All shared lines busy from ${start} to `);
    expect(busy.textContent).toContain(': 1 caller would have heard a busy signal.');
    expect(screen.getByText('Most calls at once in the last 30 days: 2.')).toBeTruthy();
  });

  it('says which numbers have calls with no price and shows them as no price, never $0', async () => {
    const advice = sharedAdvice();
    advice.pool = {
      state: 'unpriced', unpriced_numbers: ['+13035550100'],
      sentence: 'Faxbot has no price for some calls received on +13035550100, so it cannot compare shared lines yet.',
      action: "Add Telnyx's price for receiving faxes in Costs → Prices & plans.",
      numbers: [{ number: '+13035550100', kind: 'local', eligible: true, reason: null, in_pool: false, calls_before: 30,
        calls: 30, billed_by_the_minute: [], unpriced_calls: 60 }],
      assumptions: [],
    } as never;
    server.use(http.get('/routing/recommendations/receiving', () => HttpResponse.json(advice)));
    render(<ReceivingRecommendations client={client()} />);
    const lines = await screen.findByTestId('receiving-lines');
    expect(within(lines).getByTestId('receiving-action').textContent).toBe(
      "Add Telnyx's price for receiving faxes in Costs → Prices & plans.");
    const row = within(lines).getByText('+13035550100').closest('tr') as HTMLElement;
    expect(within(row).getByText('Not priced yet')).toBeTruthy();
    expect(row.textContent).not.toContain('$0.00');
    expect(within(lines).queryByRole('table', { name: 'Shared lines compared with billing by the minute' })).toBeNull();
  });

  it('writes the date a price was read as a calendar day in the reader\'s own words', () => {
    expect(readOnText('2026-10-05')).toBe(new Date(2026, 9, 5, 12).toLocaleDateString(undefined,
      { day: 'numeric', month: 'long', year: 'numeric' }));
    expect(readOnText(null)).toBe('');
  });
});
