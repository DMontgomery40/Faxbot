// Real carrier charges in the console: Delivery routes spending, the Job Details cost line,
// the Inbox cost caption, flat plans and recommendation wording. Synthetic data only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Dashboard from '../components/Dashboard';
import DeliveryRoutes from '../components/DeliveryRoutes';
import Received from '../components/Received';
import JobsList from '../components/JobsList';
import { localToday } from '../components/delivery/RateCards';
import { NOT_PRICED, formatMoney, formatMoneyList } from '../components/delivery/shared';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const usd = (amount: string) => ({ currency: 'USD', amount });

const sip = {
  provider_id: 'sip', label: 'Carrier trunk', carrier: 'Telnyx', attempts: 3, successes: 2, failures: 1, uncertain: 0,
  billed_minutes: 4, billed_pages: 4, estimated_cost: [usd('0.02')], reported_cost: [usd('0.015')], settled_cost: [],
  attempts_without_reported_cost: 1, attempts_with_reported_cost: 2, estimated_cost_not_reported: [usd('0.005')],
  awaiting_carrier_bill: 1, unmatched_charges: 0, plan: null, priced: true, total_cost: [usd('0.02')],
  unrecorded_calls: 0, unrecorded_cost: [], unrecorded_matched_to_faxes: 0,
};
const humblefax = {
  provider_id: 'humblefax', label: 'HumbleFax', carrier: null, attempts: 2, successes: 2, failures: 0, uncertain: 0,
  billed_minutes: 0, billed_pages: 3, estimated_cost: [usd('0.00')], reported_cost: [], settled_cost: [],
  attempts_without_reported_cost: 2, attempts_with_reported_cost: 0, estimated_cost_not_reported: [usd('0.00')],
  awaiting_carrier_bill: 0, unmatched_charges: 0, priced: true, total_cost: [usd('10.00')],
  plan: { label: 'HumbleFax', monthly_fee: usd('10.00'), monthly_fee_text: '$10', period_fee: usd('10.00'), period_days: 30 },
};
const documo = {
  provider_id: 'documo', label: 'Documo', carrier: null, attempts: 1, successes: 1, failures: 0, uncertain: 0,
  billed_minutes: 0, billed_pages: 1, estimated_cost: [], reported_cost: [], settled_cost: [],
  attempts_without_reported_cost: 1, attempts_with_reported_cost: 0, estimated_cost_not_reported: [],
  awaiting_carrier_bill: 0, unmatched_charges: 0, plan: null, priced: false, total_cost: [],
};
const received = {
  provider_id: 'sip', label: 'Carrier trunk', carrier: 'Telnyx', calls: 3, faxes: 2, billed_minutes: 3,
  estimated_cost: [usd('0.0096')], reported_cost: [usd('0.0064')], calls_with_reported_cost: 2, calls_without_reported_cost: 1,
  estimated_cost_not_reported: [usd('0.0032')], awaiting_carrier_bill: 0, unmatched_charges: 1,
  unrecorded_calls: 2, unrecorded_cost: [usd('0.0064')], unrecorded_matched_to_faxes: 1,
  unrecorded_unmatched_cost: [usd('0.0032')], total_cost: [usd('0.0128')],
};

function routes(costs: Record<string, unknown>, posts: unknown[] = []) {
  server.use(
    http.get('/routing/destinations', () => HttpResponse.json({ window_days: 30, destinations: [] })),
    http.get('/routing/costs', () => HttpResponse.json({ since: '2026-09-03T00:00:00', ...costs })),
    http.get('/routing/rate-cards', () => HttpResponse.json({ cards: [] })),
    http.post('/routing/reconcile', async ({ request }) => {
      posts.push(await request.text());
      return HttpResponse.json({ checked: 4, matched: 4, charges_recorded: 4, waiting: 0, ambiguous: 0,
        carrier_unavailable: false, summary: 'Checked 4 calls: 4 new charges recorded.' });
    }),
  );
  return posts;
}

describe('Delivery routes spending', () => {
  it('shows charges, estimates only for faxes not billed yet, and what is still waiting', async () => {
    const posts = routes({ providers: [sip, humblefax], received: [received],
      carrier_charges: { carrier: 'Telnyx', supported: true, readable: true } });
    render(<DeliveryRoutes client={client()} canWrite />);
    const sent = (await screen.findByText('Carrier trunk · Telnyx')).closest('.MuiCard-root') as HTMLElement;
    expect(within(sent).getByText('$0.02')).toBeTruthy();
    expect(within(sent).getByText('charged and estimated')).toBeTruthy();
    expect(within(sent).getByText('Telnyx charged $0.015 for 2 faxes.')).toBeTruthy();
    expect(within(sent).getByText('Estimated $0.005 for 1 fax not billed yet.')).toBeTruthy();
    expect(within(sent).getByText('1 fax waiting for the Telnyx bill.')).toBeTruthy();
    expect(sent.textContent).not.toMatch(/faxs/);
    expect(within(sent).getByText(/3 faxes, 2 delivered, 4 minutes, 4 pages/)).toBeTruthy();
    const plan = (await screen.findByText('HumbleFax')).closest('.MuiCard-root') as HTMLElement;
    expect(within(plan).getByText('Included in your HumbleFax plan ($10 a month)')).toBeTruthy();
    expect(within(plan).getByText(/Counted in the total: \$10\.00 for these 30 days/)).toBeTruthy();
    expect(within(plan).queryByText(/not billed yet/)).toBeNull();
    const inbound = screen.getByText('Received through Carrier trunk · Telnyx').closest('.MuiCard-root') as HTMLElement;
    expect(within(inbound).getByText('Telnyx charged $0.0064 for 4 calls, 2 without a Faxbot call record.')).toBeTruthy();
    expect(within(inbound).getByText('1 call could not be matched to one Telnyx record, so its cost is unknown.')).toBeTruthy();
    expect(within(inbound).getByText(/5 calls, 3 faxes received, 3 minutes/)).toBeTruthy();
    expect(within(inbound).getByText('Telnyx billed 1 call Faxbot has no record of: $0.0032.')).toBeTruthy();
    expect(within(inbound).getByText('1 call came in that Faxbot did not record at the time; its fax is in Received.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Check Telnyx charges now' }));
    expect(await screen.findByText('Checked 4 calls: 4 new charges recorded.')).toBeTruthy();
    expect(posts).toHaveLength(1);
  });

  it('says how to turn on charges instead of offering a check without a key', async () => {
    routes({ providers: [{ ...sip, carrier: null, reported_cost: [], attempts_with_reported_cost: 0, total_cost: [usd('0.02')],
      attempts_without_reported_cost: 3 }], received: [], carrier_charges: { carrier: 'Telnyx', supported: true, readable: false } });
    render(<DeliveryRoutes client={client()} canWrite />);
    expect(await screen.findByText('Telnyx call charges appear here once you add your Telnyx API key in Providers → Telnyx.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Check Telnyx charges now' })).toBeNull();
    expect(screen.getByText('estimated')).toBeTruthy();
  });

  it('names a flat plan and an unknown cost in the route recommendation', async () => {
    routes({ providers: [], received: [] });
    server.use(http.get('/routing/destinations/:number', () => HttpResponse.json({
      number: '+12025550123', display_name: null, notes: null, preferred_route: null, accepts_references: false, version: 0,
      estimated_cost_30_days: [], routes: [], direct_partner: null, available_routes: [],
      recommended_routes: [
        { route: 'humblefax', label: 'HumbleFax', reason: 'included', explanation: 'Included in your HumbleFax plan.',
          estimated_cost_one_page: usd('0.00'), included_in_plan: true, monthly_fee: usd('10.00') },
        { route: 'phaxio', label: 'Phaxio', reason: 'alternative', explanation: 'Used if the routes above it are unavailable.',
          estimated_cost_one_page: null, included_in_plan: false, monthly_fee: null },
      ],
    })));
    render(<DeliveryRoutes client={client()} canWrite />);
    fireEvent.change(await screen.findByLabelText('Fax number'), { target: { value: '+12025550123' } });
    fireEvent.click(screen.getByRole('button', { name: 'Look up' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('1. HumbleFax · included in your plan')).toBeTruthy();
    expect(within(dialog).getByText('Included in your HumbleFax plan.')).toBeTruthy();
    expect(within(dialog).getByText('2. Phaxio · cost unknown')).toBeTruthy();
  });

  it('saves a monthly plan fee with a rate card and shows it as included', async () => {
    const saved: unknown[] = [];
    routes({ providers: [], received: [] });
    server.use(
      http.get('/routing/rate-cards', () => HttpResponse.json({ cards: [
        { id: 'card-1', provider_id: 'humblefax', label: 'HumbleFax unlimited plan', direction: 'outbound', currency: 'USD',
          per_minute: '0.00', per_page: '0.00', per_call: '0.00', billing_increment_seconds: 60, minimum_seconds: 0,
          source_url: 'https://humblefax.com/', captured_on: '2026-10-03', monthly_fee: '10.00', included_in_plan: true },
      ] })),
      http.put('/routing/rate-cards', async ({ request }) => {
        saved.push(await request.json());
        return HttpResponse.json({ cards: [] });
      }),
    );
    render(<DeliveryRoutes client={client()} canWrite />);
    expect(await screen.findByText('$10.00 a month, faxes included')).toBeTruthy();
    expect(screen.getByText('Flat monthly fee')).toBeTruthy();  // not Whole minutes
    // The advertised date in the reader's words, never 2026-10-03.
    const advertised = new Date(2026, 9, 3).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
    expect(screen.getByText(new RegExp(`Advertised on ${advertised}`))).toBeTruthy();
    expect(document.body.textContent).not.toContain('2026-10-03');
    fireEvent.click(screen.getByRole('button', { name: 'Add rate card' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add rate card' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Plan' } });
    expect(within(dialog).getByLabelText('Billing')).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Monthly plan fee (USD)'), { target: { value: '25' } });
    // Only a monthly fee: billing increments do not apply, so they are not asked for.
    expect(within(dialog).queryByLabelText('Billing')).toBeNull();
    expect(within(dialog).queryByLabelText('Minimum seconds')).toBeNull();
    expect((within(dialog).getByLabelText('Advertised on') as HTMLInputElement).value).toBe(localToday());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(saved).toHaveLength(1));
    const cards = (saved[0] as { cards: Array<Record<string, unknown>> }).cards;
    expect(cards.find((card) => card.label === 'Plan')).toMatchObject({ monthly_fee: '25' });
  });
});

const JOB = 'b'.repeat(32);

describe('Job Details cost', () => {
  it('shows what the carrier charged for the fax', async () => {
    const job = { id: JOB, to_number: '+15550100001', status: 'success', backend: 'sip', pages: 1,
      created_at: '2026-10-03T12:00:00', updated_at: '2026-10-03T12:01:00', delivery_state: 'success',
      dispatch_mode: 'normal', delivery_version: 3 };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
      http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({
        version: 3, state: 'success', dispatch_mode: 'normal', provider_id: 'sip', profile_id: 'p', revision_id: 'r',
        attempt: null, can_bind_provider_identity: false, bind_refusal_reason: null, events: [], events_truncated: false })),
      http.get(`/routing/faxes/${JOB}/cost`, () => HttpResponse.json({ state: 'reported', reported_cost: [usd('0.005')],
        estimated_cost: [usd('0.005')], summary: 'Telnyx charged $0.005 for this call.' })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+15550100001'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect(await within(dialog).findByText('Telnyx charged $0.005 for this call.')).toBeTruthy();
    expect(within(dialog).getByText('Cost')).toBeTruthy();
  });
});

describe('Inbox cost', () => {
  const operator = new Set(['inbound:list', 'inbound:read', 'inbound:document']);
  const fax = (id: string, from: string) => ({ id, fr: from, to: '+15550100001', status: 'received', backend: 'sip', pages: 1,
    received_at: '2026-10-03T12:00:00' });

  it('reads the costs of the faxes on screen in one request and shows each under its route', async () => {
    const asked: string[] = [];
    server.use(
      http.get('/inbound', () => HttpResponse.json([fax('fax-1', '+15550101111'), fax('fax-2', '+15550102222')])),
      http.get('/routing/inbound-costs', ({ request }) => {
        asked.push(new URL(request.url).searchParams.get('ids') ?? '');
        return HttpResponse.json({ costs: {
          'fax-1': { state: 'reported', reported_cost: [usd('0.0032')], summary: 'Telnyx charged $0.0032 for this call.' },
          'fax-2': { state: 'waiting', reported_cost: [], summary: 'Cost not reported yet.' },
        } });
      }),
    );
    render(<Received client={client()} inboundEnabled permissions={operator} />);
    const first = (await screen.findByText('********1111')).closest('tr') as HTMLElement;
    expect(await within(first).findByText('Telnyx charged $0.0032 for this call.')).toBeTruthy();
    const second = screen.getByText('********2222').closest('tr') as HTMLElement;
    expect(within(second).getByText('Cost not reported yet.')).toBeTruthy();
    expect(asked).toEqual(['fax-1,fax-2']);
  });

  it('asks nothing when the Inbox is empty', async () => {
    const asked: string[] = [];
    server.use(
      http.get('/inbound', () => HttpResponse.json([])),
      http.get('/routing/inbound-costs', () => { asked.push('asked'); return HttpResponse.json({ costs: {} }); }),
    );
    render(<Received client={client()} inboundEnabled permissions={operator} />);
    expect(await screen.findByText('No received faxes yet.')).toBeTruthy();
    expect(asked).toEqual([]);
  });
});

describe('One reading of spending', () => {
  it('shows the same lines and total on the Dashboard as in Tools, from one response', async () => {
    const costs = { since: '2026-09-03T00:00:00', providers: [sip, humblefax, documo], received: [received],
      carrier_charges: { carrier: 'Telnyx', supported: true, readable: true },
      total_cost: [usd('10.0328')] };
    routes(costs);
    server.use(http.get('/routing/costs', () => HttpResponse.json(costs)));
    const { unmount } = render(<DeliveryRoutes client={client()} canWrite={false} />);
    const tools = [
      (await screen.findByText('Carrier trunk · Telnyx')).closest('.MuiCard-root')?.textContent ?? '',
      screen.getByText('HumbleFax').closest('.MuiCard-root')?.textContent ?? '',
      screen.getByText('Documo').closest('.MuiCard-root')?.textContent ?? '',
      screen.getByText('Received through Carrier trunk · Telnyx').closest('.MuiCard-root')?.textContent ?? '',
    ];
    unmount();
    render(<Dashboard client={client()} onNavigate={() => undefined} />);
    const card = await screen.findByRole('button', { name: 'Spending, last 30 days' });
    const expected = [
      ['Carrier trunk', '$0.02'],
      ['HumbleFax', 'Included in your HumbleFax plan ($10 a month)'],
      ['Documo', 'No published price; add your rate'],
      ['Received through Carrier trunk · Telnyx', '$0.0128'],
    ];
    expected.forEach(([label, value], index) => {
      expect(card.textContent).toContain(label + value);
      expect(tools[index]).toContain(value);  // the Tools card shows the very same phrase
    });
    expect(card.textContent).toContain('Total$10.03');
    expect(card.textContent).not.toContain('No price set');
  });

  it('counts faxes and calls with no price apart from every total, never as $0', async () => {
    // One sent fax estimated, one with no price (a call of unknown length); one received call with no price.
    const sent = { ...sip, attempts: 4, attempts_without_reported_cost: 2, attempts_not_priced: 1 };
    const call = { ...received, calls: 1, faxes: 1, calls_with_reported_cost: 0, calls_without_reported_cost: 1,
      calls_not_priced: 1, reported_cost: [], estimated_cost: [], estimated_cost_not_reported: [], unmatched_charges: 0,
      unrecorded_calls: 0, unrecorded_cost: [], unrecorded_matched_to_faxes: 0, unrecorded_unmatched_cost: [],
      total_cost: [] };
    const costs = { since: '2026-09-03T00:00:00', providers: [sent, { ...documo, attempts_not_priced: 1 }],
      received: [call], carrier_charges: { carrier: 'Telnyx', supported: true, readable: true },
      total_cost: [usd('0.02')], not_priced: 3 };
    routes(costs);
    const { unmount } = render(<DeliveryRoutes client={client()} canWrite={false} />);
    const trunk = (await screen.findByText('Carrier trunk · Telnyx')).closest('.MuiCard-root') as HTMLElement;
    expect(within(trunk).getByText('Estimated $0.005 for 1 fax not billed yet.')).toBeTruthy();
    expect(within(trunk).getByText('1 fax not priced yet.')).toBeTruthy();
    const inbound = screen.getByText('Received through Carrier trunk · Telnyx').closest('.MuiCard-root') as HTMLElement;
    expect(within(inbound).getByText('Not priced yet')).toBeTruthy();
    expect(within(inbound).getByText('1 call not priced yet.')).toBeTruthy();
    expect(inbound.textContent).not.toMatch(/\$0\.00|Not billed yet/);
    // A route with no published price already says so; its faxes are not counted a second time.
    const unpublished = screen.getByText('Documo').closest('.MuiCard-root') as HTMLElement;
    expect(unpublished.textContent).toContain('No published price; add your rate');
    expect(unpublished.textContent).not.toContain('not priced yet');
    unmount();
    render(<Dashboard client={client()} onNavigate={() => undefined} />);
    const card = await screen.findByRole('button', { name: 'Spending, last 30 days' });
    expect(card.textContent).toContain('Carrier trunk$0.02, 1 fax not priced yet');
    expect(card.textContent).toContain('DocumoNo published price; add your rate');
    expect(card.textContent).toContain('Received through Carrier trunk · TelnyxNot priced yet');
    expect(card.textContent).toContain('Total$0.02, 2 faxes and 1 call not priced yet');
  });
});

describe('Money as money', () => {
  it('formats the exact decimal the way the faxbot command does, and an unknown amount as not priced', () => {
    // The same cases as test_money_reads_as_the_console_shows_it: half up from the decimal, never through a float.
    expect(['0.005', '0.0032', '1.5', '0.07', '0', '10', '0.00125', '0.000049', '-0.03', '-0.000001']
      .map((amount) => formatMoney(usd(amount))))
      .toEqual(['$0.005', '$0.0032', '$1.50', '$0.07', '$0.00', '$10.00', '$0.0013', '$0.00', '-$0.03', '$0.00']);
    expect(formatMoney(usd('12345678901.125'))).toBe('$12,345,678,901.13');
    expect(formatMoney(usd('not a number'))).toBe('-');
    expect(formatMoneyList([])).toBe(NOT_PRICED);
    expect(formatMoneyList([usd('0.005'), { currency: 'EUR', amount: '0.01' }])).toBe('$0.005 + €0.01');
  });
});

describe('Rate card dates', () => {
  it('defaults the advertised date to the viewer local date, not the UTC date', () => {
    expect(localToday(new Date(2026, 9, 3, 22, 40))).toBe('2026-10-03');
    expect(localToday(new Date(2026, 0, 1, 0, 5))).toBe('2026-01-01');
  });
});

