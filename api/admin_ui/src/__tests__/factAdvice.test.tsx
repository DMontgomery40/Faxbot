// Costs → Advice (what a missing fact costs you), Numbers → Advice (whether a line can go) and Numbers → Move (a
// number's move as a checked plan). Advice only: these screens never port, enroll or change a provider account.
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import AdminAPIClient from '../api/client';
import type { FactAdvice as Advice } from '../api/factAdviceTypes';
import FactAdvice from '../components/delivery/FactAdvice';

type Request = { method: string; path: string; body?: unknown };
const usd = (amount: string) => [{ currency: 'USD', amount }];

function fakeClient(answers: Record<string, unknown>, requests: Request[] = []) {
  const client = new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
  const call = vi.fn(async (request: Request) => {
    requests.push(request);
    const key = `${request.method} ${request.path.split('?')[0]}`;
    if (!(key in answers)) throw new Error(`unexpected ${key}`);
    const answer = answers[key];
    return typeof answer === 'function' ? (answer as (request: Request) => unknown)(request) : answer;
  });
  (client as unknown as { call: typeof call }).call = call;
  return client;
}

const advice: Advice = {
  days: 90, state: 'advice', estimate: true,
  sentence: 'Establishing one fact would have made faxes to 1 recipient cheaper or priced over the last 90 days.',
  recipients: [{
    number: '+13035550124', display: '+1 303-555-0124', name: 'Synthetic Lab', kind: 'recipient', faxes: 2,
    baseline: usd('0.02'), unpriced: 0, largest: usd('0.02'),
    sentence: '2 faxes to Synthetic Lab cost $0.02 by the best route you may use (estimate).',
    facts: [{
      fact: 'toll_free', kind: 'authorization', title: 'No recorded approval for their toll-free number', boundary: true,
      saving: usd('0.02'), net: usd('0.02'), faxes: 2, establish_cost: [], establish: 'about 10 minutes of your time',
      confirm: 'Someone at Synthetic Lab must agree that you fax +1 800-555-0199; they pay for those calls.',
      step: 'Record who agreed and when under Recipients → Details.', chance: null, break_even: null, unknown: false,
      realized: false,
      sentence: 'Would have cost $0.02 less over 2 faxes (estimate). Establishing it takes about 10 minutes of your time. Faxbot will not use it until then.',
    }],
  }],
  realized: 'These figures are what your faxes would have cost, not savings. Once a fact is established and used, Costs → Savings counts what it really saved.',
  note: 'Faxbot only advises: it never enrolls a partner, records an approval, enters a price or changes a route for you.',
  catalogue: [], assumptions: ["Each fax of the last 90 days is priced again at today's prices and plan use, by its page count."],
};

describe('Costs → Advice', () => {
  it('shows each recipient with its facts, who must confirm, and that these are not savings', async () => {
    const requests: Request[] = [];
    render(<FactAdvice client={fakeClient({ 'GET /routing/recommendations/facts': advice }, requests)} />);
    await waitFor(() => expect(screen.getByTestId('fact-advice-sentence').textContent).toMatch(/1\ recipient\ cheaper/));
    expect(screen.getByText('Synthetic Lab (+1 303-555-0124)')).toBeTruthy();
    expect(screen.getByText('Up to $0.02 less (estimate)')).toBeTruthy();
    expect(screen.getByText('Needs their agreement')).toBeTruthy();
    expect(screen.getByTestId('fact-toll_free').textContent).toMatch(/Faxbot\ will\ not\ use\ it\ until\ then\./);
    expect(screen.getByText(/they pay for those calls/)).toBeTruthy();
    expect(screen.getByText(/not savings/)).toBeTruthy();
    expect(requests.map((request) => request.path)).toEqual(['/routing/recommendations/facts']);
  });

  it('says so plainly when the advice cannot be read', async () => {
    render(<FactAdvice client={fakeClient({})} />);
    await waitFor(() => expect(screen.getByText(/couldn't work out this advice/)).toBeTruthy());
  });
});
