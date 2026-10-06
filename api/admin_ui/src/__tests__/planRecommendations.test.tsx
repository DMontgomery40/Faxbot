import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import PlanRecommendations from '../components/delivery/PlanRecommendations';
import Recommendations, { NO_RECOMMENDATIONS } from '../components/delivery/Recommendations';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const usd = (amount: string) => [{ currency: 'USD', amount }];
const window = (days: number, sent: number, extra: Record<string, unknown> = {}) => ({
  start: '2026-09-05T00:00:00', end: '2026-10-05T00:00:00', days, sent, received: 0, own_numbers: 0,
  fee: days ? usd('10.00') : [], fee_per_fax: sent ? usd('2.00') : [], other_way: sent ? usd('0.05') : [],
  number_rental: days ? usd('1.00') : [], other_routes: sent ? ['Telnyx'] : [], without_other_way: 0, ...extra,
});

// Synthetic: HumbleFax's $10 plan carried 5 faxes that Telnyx would have sent for about $0.05.
function review() {
  return {
    days: 30, estimate: true, empty_sentence: null,
    plans: [{
      route: 'humblefax', name: 'HumbleFax', monthly_fee: usd('10.00'), state: 'review', estimate: true,
      sentence: 'Worth reviewing: HumbleFax carried 5 faxes in the last 30 days, about $2.00 each for its $10 monthly '
        + 'fee; Telnyx would have cost about $0.05 for the same faxes and $1.00 to keep the number, $8.95 less (estimate).',
      action: 'If you decide to drop the plan, fax these numbers with Telnyx instead, then cancel the plan in your '
        + 'HumbleFax account. Faxbot never cancels anything for you.',
      caveats: ['Before you cancel, move your HumbleFax number to Telnyx if anyone still faxes it.'],
      windows: [window(30, 5), window(0, 0)],
    }],
  };
}

describe('Costs → Recommendations → Plans', () => {
  it('says there is no plan to review when no fax service has a monthly fee', async () => {
    render(<PlanRecommendations client={client()} />);
    expect((await screen.findByTestId('plans-empty')).textContent)
      .toBe('You pay no monthly fee for a fax service, so there is no plan to review.');
  });

  it('shows a plan worth reviewing with its fee, what to do, its caveat and both periods as estimates', async () => {
    server.use(http.get('/routing/recommendations/plans', () => HttpResponse.json(review())));
    render(<PlanRecommendations client={client()} />);
    const plan = await screen.findByTestId('plan-recommendation');
    expect(within(plan).getByText('HumbleFax, $10.00 a month')).toBeTruthy();
    expect(within(plan).getByText('Worth reviewing')).toBeTruthy();
    expect(within(plan).getAllByText('Estimate').length).toBeGreaterThan(0);
    expect(within(plan).getByTestId('plan-sentence').textContent).toContain('$8.95 less (estimate)');
    expect(within(plan).getByTestId('plan-action').textContent).toContain('Faxbot never cancels anything for you.');
    expect(within(plan).getByText(/move your HumbleFax number to Telnyx/)).toBeTruthy();
    const table = within(plan).getByRole('table', { name: 'HumbleFax plan compared with paying per fax' });
    const perFax = within(table).getByText('Plan fee per fax').closest('tr') as HTMLElement;
    // The 30 days before have no records: a dash, never $0.
    expect(within(perFax).getAllByRole('cell').map((cell) => cell.textContent)).toEqual(['Plan fee per fax', '$2.00', '-']);
    expect(document.body.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T|\btrue\b|\bfalse\b|humblefax/);
  });

  it('shows too little history as its sentence alone, and the page keeps its empty sentence', async () => {
    const advice = review();
    Object.assign(advice.plans[0], {
      state: 'too_little_history', action: null, caveats: [],
      sentence: 'Not enough history yet to judge your $10 HumbleFax plan: Faxbot has records for 2 days, and HumbleFax '
        + 'carried 1 fax in that time.',
    });
    server.use(http.get('/routing/recommendations/plans', () => HttpResponse.json(advice)));
    render(<Recommendations client={client()} canWrite />);
    expect((await screen.findByTestId('plan-sentence')).textContent).toContain('Not enough history yet');
    expect(within(screen.getByTestId('plan-recommendation')).queryByRole('table')).toBeNull();
    expect((await screen.findByTestId('recommendations-empty')).textContent).toBe(NO_RECOMMENDATIONS);
  });

  it('a plan Faxbot can judge counts as a recommendation, so the empty sentence goes', async () => {
    server.use(http.get('/routing/recommendations/plans', () => HttpResponse.json(review())));
    render(<Recommendations client={client()} canWrite />);
    await screen.findByTestId('plan-recommendation');
    expect(screen.queryByTestId('recommendations-empty')).toBeNull();
  });
});
