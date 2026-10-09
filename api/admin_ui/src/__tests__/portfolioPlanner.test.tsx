import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import AdminAPIClient, { AdminAPIError } from '../api/client';
import PortfolioPlanner from '../components/delivery/PortfolioPlanner';

type Input = { currency: string; horizon: string; perspective: string; budget: string | null;
  nodes: Array<{ id: string; label: string; cost: string | null; installed: boolean; group_id: string | null }>;
  groups: Array<{ id: string; label: string; cost: string | null }>;
  relationships: Array<{ a: string; b: string; expected: string | null; cautious: string | null }> };
type Request = { method: string; path: string; body?: Input };

function fake(answer: (input: Input) => unknown = planned) {
  const client = new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
  const call = vi.fn(async (request: Request) => answer(request.body!));
  (client as unknown as { call: typeof call }).call = call;
  return { client, call };
}

function planned(input: Input, abstain = false) {
  const selected = input.nodes.map(node => node.id);
  const plan = { state: abstain ? 'abstain' : 'planned', installed_ids: [] as string[],
    new_ids: abstain ? [] : selected, selected_ids: abstain ? [] : selected,
    incremental_cost: abstain ? '0' : '23', expected_benefit: abstain ? '0' : '100',
    cautious_benefit: abstain ? '0' : '40', expected_net: abstain ? '0' : '77',
    cautious_net: abstain ? '0' : '17', objective: abstain ? '0' : '77' };
  return { state: abstain ? 'abstain' : 'planned', currency: input.currency, horizon: input.horizon,
    perspective: input.perspective, budget: input.budget, missing: [],
    plans: { expected: plan, cautious: { ...plan, objective: abstain ? '0' : '17' } },
    estimate: true, realized: false, saved: false, note: 'No settings changed.', assumptions: ['Synthetic supplied estimates.'] };
}

function fill(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
function overview() {
  fill('Currency', 'USD'); fill('Planning period', 'Next 12 months'); fill('Whose costs and benefits?', 'Our company');
  fill('Budget', '100');
}
function addItem(name: string, cost = '') {
  fireEvent.click(screen.getByRole('button', { name: 'Add setup item' }));
  const items = screen.getAllByRole('group', { name: /Setup item / });
  const row = within(items[items.length - 1]);
  fireEvent.change(row.getByLabelText('Name'), { target: { value: name } });
  if (cost) fireEvent.change(row.getByLabelText('Setup cost'), { target: { value: cost } });
  return row;
}
function ready() { overview(); addItem('North office', '10'); addItem('South office', '13'); }
const calculate = () => fireEvent.click(screen.getByRole('button', { name: 'Compare setup plans' }));

describe('Costs → Advice setup planner', () => {
  it('starts empty without assumed prices or currency and does not make a request', () => {
    const { client, call } = fake(); render(<PortfolioPlanner client={client} />);
    expect((screen.getByLabelText('Currency') as HTMLInputElement).value).toBe('');
    expect(screen.getByText('Inputs are not saved.')).toBeTruthy();
    expect(screen.getByText(/Add the setup items you want to compare/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Compare setup plans' }) as HTMLButtonElement).disabled).toBe(true);
    expect(call).not.toHaveBeenCalled();
  });

  it('posts explicit inputs with unknown amounts as null, displays incomplete fields using names', async () => {
    const { client, call } = fake(input => ({ ...planned(input), state: 'incomplete', plans: null,
      missing: [{ field: 'nodes[0].cost', reason: 'Setup cost is unknown.' }] }));
    render(<PortfolioPlanner client={client} />); overview(); fill('Budget', ''); addItem('North office'); calculate();
    expect(await screen.findByText(/More information is needed/)).toBeTruthy();
    expect(screen.getByText(/North office.*Setup cost is unknown/)).toBeTruthy();
    expect(call.mock.calls[0][0]).toMatchObject({ method: 'POST', path: '/routing/portfolio/plan',
      body: { currency: 'USD', horizon: 'Next 12 months', perspective: 'Our company', budget: null,
        nodes: [{ label: 'North office', cost: null, installed: false, group_id: null }], groups: [], relationships: [] } });
    expect(screen.queryByRole('region', { name: 'Expected plan' })).toBeNull();
  });

  it('compares bundles with a shared setup charge once and keeps IDs out of the result', async () => {
    const { client, call } = fake(); render(<PortfolioPlanner client={client} />); overview();
    fireEvent.click(screen.getByRole('button', { name: 'Add shared cost' }));
    const group = within(screen.getByRole('group', { name: 'Shared cost 1' }));
    fireEvent.change(group.getByLabelText('Name'), { target: { value: 'Partner connection' } });
    fireEvent.change(group.getByLabelText('Shared setup cost'), { target: { value: '20' } });
    const first = addItem('North office', '1'); const second = addItem('South office', '2');
    for (const row of [first, second]) {
      fireEvent.mouseDown(row.getByLabelText('Shared cost'));
      fireEvent.click(screen.getByRole('option', { name: 'Partner connection' }));
    }
    fireEvent.click(screen.getByRole('button', { name: 'Add relationship' }));
    const relationship = within(screen.getByRole('group', { name: 'Relationship 1' }));
    for (const [label, name] of [['First item', 'North office'], ['Second item', 'South office']]) {
      fireEvent.mouseDown(relationship.getByLabelText(label)); fireEvent.click(screen.getByRole('option', { name }));
    }
    fireEvent.change(relationship.getByLabelText('Expected benefit'), { target: { value: '100.123456' } });
    fireEvent.change(relationship.getByLabelText('Cautious benefit'), { target: { value: '40' } });
    calculate();
    const result = within(await screen.findByRole('region', { name: 'Expected plan' }));
    expect(result.getAllByText(/Partner connection/)).toHaveLength(1);
    expect(result.getByText(/North office/)).toBeTruthy();
    expect(result.getByText(/Expected net benefit/).parentElement?.textContent).toContain('77');
    expect(screen.getByRole('region', { name: 'Cautious plan' })).toBeTruthy();
    const body = call.mock.calls[0][0].body!;
    expect(body.relationships[0].expected).toBe('100.123456');
    expect(body.groups).toHaveLength(1);
    expect(body.nodes[0].group_id).toBe(body.groups[0].id);
    expect(screen.getByRole('region', { name: 'Expected plan' }).textContent).not.toContain(body.nodes[0].id);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('keeps already installed items and removes relationships when an endpoint is removed', async () => {
    const { client, call } = fake(input => planned(input, true)); render(<PortfolioPlanner client={client} />); ready();
    const items = screen.getAllByRole('group', { name: /Setup item / });
    fireEvent.click(within(items[0]).getByLabelText('Already installed'));
    fireEvent.click(screen.getByRole('button', { name: 'Add relationship' }));
    const row = within(screen.getByRole('group', { name: 'Relationship 1' }));
    fireEvent.mouseDown(row.getByLabelText('First item')); fireEvent.click(screen.getByRole('option', { name: 'North office' }));
    fireEvent.mouseDown(row.getByLabelText('Second item')); fireEvent.click(screen.getByRole('option', { name: 'South office' }));
    fireEvent.click(within(items[1]).getByRole('button', { name: 'Remove setup item' }));
    calculate(); await screen.findByRole('region', { name: 'Expected plan' });
    expect(call.mock.calls[0][0].body?.relationships).toEqual([]);
    expect(call.mock.calls[0][0].body?.nodes[0].installed).toBe(true);
    expect(screen.getAllByText(/No additional setup/)).toHaveLength(2);
  });

  it('invalidates a displayed result when any input changes', async () => {
    const { client } = fake(); render(<PortfolioPlanner client={client} />); ready(); calculate();
    await screen.findByRole('region', { name: 'Expected plan' }); fill('Budget', '40');
    expect(screen.queryByRole('region', { name: 'Expected plan' })).toBeNull();
  });

  it('preserves signed benefits and exact returned decimals without rounding', async () => {
    const { client, call } = fake(input => {
      const result = planned(input);
      result.plans.expected.expected_net = '999999999999.123456';
      result.plans.expected.cautious_net = '-0.000001';
      return result;
    });
    render(<PortfolioPlanner client={client} />); ready();
    fireEvent.click(screen.getByRole('button', { name: 'Add relationship' }));
    const row = within(screen.getByRole('group', { name: 'Relationship 1' }));
    fireEvent.mouseDown(row.getByLabelText('First item')); fireEvent.click(screen.getByRole('option', { name: 'North office' }));
    fireEvent.mouseDown(row.getByLabelText('Second item')); fireEvent.click(screen.getByRole('option', { name: 'South office' }));
    fireEvent.change(row.getByLabelText('Expected benefit'), { target: { value: '999999999999.123456' } });
    fireEvent.change(row.getByLabelText('Cautious benefit'), { target: { value: '-0.000001' } });
    calculate();
    const result = within(await screen.findByRole('region', { name: 'Expected plan' }));
    expect(result.getByText('USD 999999999999.123456')).toBeTruthy();
    expect(result.getByText('USD -0.000001')).toBeTruthy();
    expect(call.mock.calls[0][0].body?.relationships[0]).toMatchObject({ expected: '999999999999.123456', cautious: '-0.000001' });
  });

  it('does not show sunk shared costs as additional spending', async () => {
    const { client, call } = fake(input => {
      const result = planned(input);
      for (const plan of Object.values(result.plans)) {
        plan.installed_ids = [input.nodes[0].id]; plan.new_ids = [input.nodes[1].id]; plan.incremental_cost = '13';
      }
      return result;
    });
    render(<PortfolioPlanner client={client} />); ready();
    fireEvent.click(screen.getByRole('button', { name: 'Add shared cost' }));
    const group = within(screen.getByRole('group', { name: 'Shared cost 1' }));
    fireEvent.change(group.getByLabelText('Name'), { target: { value: 'Installed connection' } });
    const rows = screen.getAllByRole('group', { name: /Setup item / });
    fireEvent.click(within(rows[0]).getByLabelText('Already installed'));
    fireEvent.change(within(rows[0]).getByLabelText('Setup cost'), { target: { value: '' } });
    for (const row of rows) {
      fireEvent.mouseDown(within(row).getByLabelText('Shared cost'));
      fireEvent.click(screen.getByRole('option', { name: 'Installed connection' }));
    }
    calculate();
    const result = within(await screen.findByRole('region', { name: 'Expected plan' }));
    expect(result.getByText(/Already installed: North office/)).toBeTruthy();
    expect(result.queryByText(/Installed connection/)).toBeNull();
    expect(result.getByText(/already covered by an installed item/)).toBeTruthy();
    expect(call.mock.calls[0][0].body?.groups[0].cost).toBeNull();
    fireEvent.click(group.getByRole('button', { name: 'Remove shared cost' }));
    expect(screen.queryByRole('region', { name: 'Expected plan' })).toBeNull();
    calculate(); await screen.findByRole('region', { name: 'Expected plan' });
    expect(call.mock.calls[1][0].body?.groups).toEqual([]);
    expect(call.mock.calls[1][0].body?.nodes.map(node => node.group_id)).toEqual([null, null]);
  });

  it('rejects reversed duplicate pairs before they can double count a benefit', () => {
    const { client, call } = fake(); render(<PortfolioPlanner client={client} />); ready();
    for (const pair of [['North office', 'South office'], ['South office', 'North office']]) {
      fireEvent.click(screen.getByRole('button', { name: 'Add relationship' }));
      const rows = screen.getAllByRole('group', { name: /Relationship / });
      const row = within(rows[rows.length - 1]);
      fireEvent.mouseDown(row.getByLabelText('First item')); fireEvent.click(screen.getByRole('option', { name: pair[0] }));
      fireEvent.mouseDown(row.getByLabelText('Second item')); fireEvent.click(screen.getByRole('option', { name: pair[1] }));
    }
    calculate(); expect(screen.getByText(/enter each pair only once/)).toBeTruthy(); expect(call).not.toHaveBeenCalled();
  });

  it('ignores a late result after editing and after a newer result completes', async () => {
    let finish!: (value: unknown) => void; let firstInput!: Input;
    const { client, call } = fake(input => {
      if (!firstInput) { firstInput = input; return new Promise(resolve => { finish = resolve; }); }
      return planned(input, true);
    });
    render(<PortfolioPlanner client={client} />); ready(); calculate();
    expect(screen.getByRole('progressbar')).toBeTruthy(); fill('Budget', '0'); calculate();
    await screen.findByRole('region', { name: 'Expected plan' });
    await act(async () => { finish(planned(firstInput)); });
    expect(screen.getAllByText(/No additional setup/)).toHaveLength(2);
    expect(call).toHaveBeenCalledTimes(2);
  });

  it.each([[403, /do not have permission/], [500, /couldn't compare these plans/]])('shows a usable %s error and allows retry', async (status, message) => {
    let failure = true;
    const { client } = fake(input => { if (failure) throw new AdminAPIError(status as number, 'Failure'); return planned(input); });
    render(<PortfolioPlanner client={client} />); ready(); calculate();
    expect(await screen.findByText(message as RegExp)).toBeTruthy(); failure = false; calculate();
    await screen.findByRole('region', { name: 'Expected plan' });
    expect(screen.queryByText(message as RegExp)).toBeNull();
  });

  it('caps setup items and shared costs at ten and rejects invalid amounts locally', async () => {
    const { client, call } = fake(); render(<PortfolioPlanner client={client} />); overview();
    for (let i = 0; i < 10; i++) addItem(`Office ${i}`, '1');
    expect((screen.getByRole('button', { name: 'Add setup item' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(screen.getAllByRole('group', { name: /Setup item / })[0]).getByLabelText('Setup cost'), { target: { value: '-1' } });
    calculate(); expect(await screen.findByText(/Use nonnegative amounts/)).toBeTruthy(); expect(call).not.toHaveBeenCalled();
    for (let i = 0; i < 10; i++) fireEvent.click(screen.getByRole('button', { name: 'Add shared cost' }));
    expect((screen.getByRole('button', { name: 'Add shared cost' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
