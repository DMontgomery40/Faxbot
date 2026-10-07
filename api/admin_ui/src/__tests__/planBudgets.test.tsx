import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { PlanContract } from '../api/deliveryTypes';
import PlanBudgets, { planDraft, planEntry, withPlanEntry } from '../components/delivery/PlanBudgets';
import { settingsFixture, receipt } from '../test/settingsFixture';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const usd = (amount: string) => [{ currency: 'USD', amount }];

// Synthetic: HumbleFax's $10 plan has carried 210 of a 200-page budget since 1 October; Telnyx faxed its number twice.
function humblefax(changes: Partial<PlanContract> = {}): PlanContract {
  return {
    route: 'humblefax', name: 'HumbleFax', currency: 'USD', estimate: true, monthly_fee: usd('10.00'), kind: 'flat',
    budget: { pages: 200, faxes: 50, day: 1, included_pages: null, page_overage: [], included_minutes: null,
      per_minute: [], commitment: [], source: 'default',
      sentence: "Faxbot starts HumbleFax at 200 pages and 50 faxes a month because HumbleFax's terms keep unlimited "
        + 'faxing for normal, individual use without naming a number; this is a cautious start, not a limit HumbleFax '
        + 'has promised to accept.' },
    period: { start: '2026-10-01T00:00:00', end: '2026-11-01T00:00:00', first_day: '2026-10-01',
      next_day: '2026-11-01', next_day_text: '1 November' },
    used: { sent_faxes: 30, sent_pages: 200, received_faxes: 4, received_pages: 10, faxes: 34, pages: 210, minutes: null,
      spend: [], not_priced: 0, counted_by_pages_only: 30 },
    left: { pages: -10, faxes: 16, allowance: null, minutes: null, commitment: [] },
    overage: { pages: 0, minutes: 0, cost: usd('0.00'), cost_unknown: false },
    committed: usd('10.00'), bill_so_far: usd('10.00'),
    bill_sentence: 'Committed this period with HumbleFax: the $10 plan fee; nothing past it so far.',
    state: 'over_budget', over: true,
    sentence: 'HumbleFax has carried 210 pages and 34 faxes since 1 October, past your normal-use budget of 200 pages '
      + 'and 50 faxes; the budget starts again on 1 November.',
    pace_sentence: 'At this pace HumbleFax will carry about 320 pages by 1 November, past your normal-use budget of 200.',
    count_sentence: 'HumbleFax counts each document page or each 60 seconds on the line, whichever is more, and Faxbot '
      + 'counts the same way wherever it knows the time on the line.',
    untimed_sentence: 'HumbleFax also counts each started minute on the line as a page, but the time on the line was not '
      + "known for 30 of these faxes, so Faxbot counted pages only and HumbleFax's own count may be higher.",
    burn_down: [{ date: '2026-10-01', pages: 12, faxes: 3 }, { date: '2026-10-02', pages: 30, faxes: 6 }],
    own_accounts: [{ direction: 'received', other: 'Telnyx', faxes: 2, pages: 6, sending_bill: 'Telnyx',
      receiving_bill: 'HumbleFax', sending_cost: usd('0.01'),
      sentence: '2 faxes from Telnyx came to your HumbleFax number: Telnyx billed the calls (about $0.01, estimate), and '
        + 'HumbleFax received them inside your plan.' }],
    ...changes,
  };
}

function reply(plans: PlanContract[], text = '') {
  return { plans, estimate: true, plan_budgets: text, empty_sentence: null };
}

describe('Costs → Prices & plans → Your plans this month', () => {
  it('says there is no plan to show when nothing has a fee, allowance or commitment', async () => {
    render(<PlanBudgets client={client()} canWrite />);
    expect((await screen.findByTestId('plan-budgets-empty')).textContent).toBe(
      'You pay no monthly fee for a fax service and set no allowance or commitment, so there is no plan to show.');
  });

  it('shows a plan over its budget with what is left, what is committed, whose bill and each day', async () => {
    server.use(http.get('/routing/plans', () => HttpResponse.json(reply([humblefax()]))));
    render(<PlanBudgets client={client()} canWrite={false} />);
    const plan = await screen.findByTestId('plan-budget');
    expect(within(plan).getByText('HumbleFax, $10.00 a month')).toBeTruthy();
    expect(within(plan).getByText('Over your normal-use budget')).toBeTruthy();
    expect(within(plan).getAllByText('Estimate').length).toBeGreaterThan(0);
    expect(within(plan).getByTestId('plan-budget-sentence').textContent).toContain('past your normal-use budget');
    expect(within(plan).getByTestId('plan-budget-progress').textContent)
      .toBe('210 of 200 pages of your normal-use budget used');
    const table = within(plan).getByRole('table', { name: 'HumbleFax this billing period' });
    const cells = (label: string) => within(within(table).getByText(label).closest('tr') as HTMLElement)
      .getAllByRole('cell').map((cell) => cell.textContent);
    // Past the budget: nothing left, never a negative count.
    expect(cells('Left of the budget')).toEqual(['Left of the budget', '0 pages and 16 faxes']);
    expect(cells('Committed this period')).toEqual(['Committed this period', '$10.00']);
    expect(cells('Counts start again')[1]).toMatch(/2026/);
    expect(within(plan).getByText(/Committed this period with HumbleFax/)).toBeTruthy();
    expect(within(plan).getByText(/whichever is more/)).toBeTruthy();
    // Sent HumbleFax faxes have no known time on the line: the screen says they were counted by pages only.
    expect(within(plan).getByText(/counted pages only/)).toBeTruthy();
    expect(within(plan).getByTestId('plan-own-accounts').textContent).toContain('Telnyx billed the calls');
    fireEvent.click(within(plan).getByText(/Day by day since/));
    const days = within(plan).getByRole('table', { name: 'HumbleFax day by day' });
    expect(within(days).getAllByRole('row')).toHaveLength(3);
    // Read only: no way to change the budget.
    expect(within(plan).queryByRole('button', { name: 'Change the HumbleFax budget' })).toBeNull();
    expect(document.body.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T|\btrue\b|\bfalse\b|plan_budgets|humblefax:/);
  });

  it('saves a new budget as the plan_budgets setting, keeping the other plans', async () => {
    const writes: Array<Record<string, unknown>> = [];
    let text = 'efax:included_pages=200';
    server.use(
      http.get('/routing/plans', () => HttpResponse.json(reply([humblefax()], text))),
      http.get('/admin/settings', () => HttpResponse.json(settingsFixture())),
      http.put('/admin/settings', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push(body);
        text = String(body.plan_budgets);
        return HttpResponse.json(receipt('rev-2'));
      }),
    );
    render(<PlanBudgets client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Change the HumbleFax budget' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Pages a month'), { target: { value: '500' } });
    fireEvent.change(within(dialog).getByLabelText('Faxes a month'), { target: { value: '' } });
    fireEvent.change(within(dialog).getByLabelText('Billing day'), { target: { value: '15' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ expected_revision_id: expect.any(String),
      plan_budgets: 'efax:included_pages=200; humblefax:pages=500,faxes=none,day=15' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('refuses a budget that is not a whole number before saving', async () => {
    server.use(http.get('/routing/plans', () => HttpResponse.json(reply([humblefax()]))));
    render(<PlanBudgets client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Change the HumbleFax budget' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Pages a month'), { target: { value: 'lots' } });
    expect(within(dialog).getByTestId('plan-budget-problem').textContent)
      .toBe('Budgets and allowances are whole numbers from 1 to 1,000,000; leave a field empty for none.');
    expect((within(dialog).getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('goes back to the starting budget for a budget you set', async () => {
    const writes: Array<Record<string, unknown>> = [];
    const set = humblefax({ budget: { ...humblefax().budget, source: 'set', sentence: 'You set this budget for HumbleFax.' } });
    server.use(
      http.get('/routing/plans', () => HttpResponse.json(reply([set], 'humblefax:pages=500; efax:day=3'))),
      http.get('/admin/settings', () => HttpResponse.json(settingsFixture())),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json(receipt('rev-2'));
      }),
    );
    render(<PlanBudgets client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: "Use Faxbot's starting budget" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0].plan_budgets).toBe('efax:day=3');
  });

  it('builds one plan entry from the form, with an empty budget meaning no limit', () => {
    const allowance = humblefax({ route: 'efax', name: 'eFax', kind: 'allowance', budget: { ...humblefax().budget,
      pages: null, faxes: null, included_pages: 200, page_overage: usd('0.10'), commitment: [], day: 3 } });
    expect(planEntry(planDraft(allowance))).toBe('pages=none,faxes=none,day=3,included_pages=200,page_overage=0.10');
    expect(withPlanEntry('humblefax:pages=200; efax:day=1', 'efax', 'day=9')).toBe('humblefax:pages=200; efax:day=9');
    expect(withPlanEntry('', 'sip', 'commitment=50')).toBe('sip:commitment=50');
  });
});
