// Cost per delivered fax: the Recipients list and Details, Costs → Recommendations → Sending
// with "Use this route", and the reason a sent fax went by its route.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { DeliveredCost, Destination, SendingRecommendation } from '../api/deliveryTypes';
import Destinations from '../components/delivery/Destinations';
import Recommendations, { NO_RECOMMENDATIONS } from '../components/delivery/Recommendations';
import JobsList from '../components/JobsList';
import { newReceivingAdvice, server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+12025550123';

function figure(route: string, label: string, changes: Partial<DeliveredCost> = {}): DeliveredCost {
  return {
    route, label, attempts: 14, delivered: 9, failed: 5, uncertain: 0, cancelled: 0, delivered_percent: 64,
    cost_per_delivered: { currency: 'USD', amount: '0.008889' }, total_cost: { currency: 'USD', amount: '0.08' },
    estimate: false, charged_attempts: 14, estimated_attempts: 0, unpriced_attempts: 0, included_in_plan: false,
    direct: false, average_pages: 1.9, average_connected_seconds: 37, enough_evidence: true, cost_text: '$0.0089',
    basis_text: '14 faxes billed', ...changes,
  };
}

const TELNYX = figure('sip', 'Telnyx');
const HUMBLEFAX = figure('humblefax', 'HumbleFax', {
  attempts: 4, delivered: 4, failed: 0, delivered_percent: 100, cost_per_delivered: null, total_cost: null,
  estimated_attempts: 4, charged_attempts: 0, included_in_plan: true, average_pages: 2, average_connected_seconds: null,
  enough_evidence: false, cost_text: 'Included in your plan', basis_text: null,
});

const destination: Destination = {
  number: NUMBER, display_name: 'County clinic', notes: null, preferred_route: 'sip', accepts_references: false,
  version: 4, estimated_cost_30_days: [{ currency: 'USD', amount: '0.08' }],
  routes: [{ route: 'sip', label: 'Telnyx', attempts: 14, successes: 9, failures: 5, uncertain: 0, success_percent: 64,
    estimated_cost_30_days: [{ currency: 'USD', amount: '0.08' }], reported_cost_30_days: [], last_attempt_at: null }],
  delivered_costs: [TELNYX, HUMBLEFAX],
};

describe('Recipients', () => {
  it('lists each route by cost per delivered fax and explains the figures in Details', async () => {
    server.use(http.get('/routing/destinations/:number', () => HttpResponse.json({
      ...destination, direct_partner: null, recommended_routes: [], available_routes: [{ route: 'sip', label: 'Telnyx' }],
    })));
    render(<Destinations client={client()} destinations={[destination]} canWrite onChanged={() => undefined} />);
    expect(screen.getByRole('columnheader', { name: 'Per delivered fax' })).toBeTruthy();
    const row = screen.getByText('County clinic').closest('tr') as HTMLElement;
    expect(within(row).getByText('Telnyx: $0.0089 · HumbleFax: Included in your plan')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: `Details for ${NUMBER}` }));
    const section = await screen.findByTestId('delivered-costs');
    expect(within(section).getByText('Cost per delivered fax, last 30 days')).toBeTruthy();
    expect(within(section).getByText('Telnyx: $0.0089 per delivered fax')).toBeTruthy();
    expect(within(section).getByText(
      '9 of 14 delivered, $0.08 in all (14 faxes billed). Average 1.9 pages, 37 seconds a call.')).toBeTruthy();
    // A flat plan is in the plan, never a price per fax.
    expect(within(section).getByText('HumbleFax: Included in your plan')).toBeTruthy();
    expect(within(section).getByText('4 of 4 delivered. Average 2 pages.')).toBeTruthy();
  });
});

const recommendation: SendingRecommendation = {
  number: NUMBER, display_name: 'County clinic', version: 4, preferred_route: 'signalwire', chosen_by_you: true,
  kind: 'cheaper_route', current: figure('signalwire', 'SignalWire', { cost_text: '$0.13', delivered: 4, attempts: 5 }),
  suggested: figure('sip', 'Telnyx', { delivered: 9, attempts: 14 }),
  saving_per_fax: { currency: 'USD', amount: '0.1211' },
  sentence: 'Telnyx cost $0.0089 per delivered fax to this number over the last 30 days. SignalWire, which you chose, cost $0.13.',
};

describe('Costs → Recommendations', () => {
  it('shows the empty sentence only when no section has anything', async () => {
    const { unmount } = render(<Recommendations client={client()} canWrite />);
    expect((await screen.findByTestId('recommendations-empty')).textContent).toBe(NO_RECOMMENDATIONS);
    expect(screen.queryByTestId('sending-recommendations')).toBeNull();
    unmount();
    // Receiving has advice (enough call history): no empty sentence, even with nothing to send cheaper.
    const advice = newReceivingAdvice();
    server.use(http.get('/routing/recommendations/receiving', () => HttpResponse.json({ ...advice,
      pool: { ...advice.pool, state: 'keep_metered', sentence: 'Keep every number billed by the minute.' },
      quiet_numbers: { ...advice.quiet_numbers, state: 'none_quiet', sentence: 'Every number gets calls.' } })));
    render(<Recommendations client={client()} canWrite />);
    expect(await screen.findByText('Keep every number billed by the minute.')).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId('recommendations-empty')).toBeNull();
  });

  it('lists a cheaper route and uses it as the preferred route with the existing setting', async () => {
    const patched: unknown[] = [];
    let items = [recommendation];
    server.use(
      http.get('/routing/recommendations/sending', () => HttpResponse.json({ window_days: 30, min_delivered: 3, items,
        empty_sentence: 'Nothing to suggest yet.' })),
      http.patch('/routing/destinations/:number', async ({ request }) => {
        patched.push(await request.json());
        items = [];
        return HttpResponse.json({ ...destination, preferred_route: 'sip', version: 5 });
      }),
    );
    render(<Recommendations client={client()} canWrite />);
    const card = await screen.findByTestId('sending-recommendation');
    expect(within(card).getByText(recommendation.sentence)).toBeTruthy();
    expect(within(card).getByText('SignalWire: 4 of 5 faxes delivered')).toBeTruthy();
    expect(within(card).getByText('Telnyx: 9 of 14 faxes delivered')).toBeTruthy();
    expect(within(card).getByText('Saves about $0.12 a fax')).toBeTruthy();
    expect(screen.queryByTestId('recommendations-empty')).toBeNull();
    fireEvent.click(within(card).getByRole('button', { name: `Use Telnyx for ${NUMBER}` }));
    expect(await screen.findByText('Telnyx is now the first choice for faxes to County clinic.')).toBeTruthy();
    expect(patched).toEqual([{ preferred_route: 'sip', version: 4 }]);
    // Nothing left to suggest: the empty sentence comes back.
    expect((await screen.findByTestId('recommendations-empty')).textContent).toBe(NO_RECOMMENDATIONS);
  });

  it('words a flat plan as the plan, and needs the write permission to change a route', async () => {
    const plan: SendingRecommendation = {
      ...recommendation, kind: 'plan', suggested: HUMBLEFAX, saving_per_fax: null,
      sentence: 'Your HumbleFax plan already includes faxes to this number. Telnyx cost $0.0089 per delivered fax here over the last 30 days.',
    };
    server.use(http.get('/routing/recommendations/sending', () => HttpResponse.json({ window_days: 30, min_delivered: 3,
      items: [plan], empty_sentence: 'Nothing to suggest yet.' })));
    render(<Recommendations client={client()} canWrite={false} />);
    const card = await screen.findByTestId('sending-recommendation');
    expect(within(card).getByText(plan.sentence)).toBeTruthy();
    expect(within(card).queryByText(/Saves about/)).toBeNull();
    expect((within(card).getByRole('button', { name: `Use HumbleFax for ${NUMBER}` }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('Sent details', () => {
  it('says why the fax went by its route', async () => {
    const JOB = 'c'.repeat(32);
    const job = { id: JOB, to_number: '+15550100001', status: 'SUCCESS', backend: 'phaxio', pages: 1,
      created_at: '2026-10-03T12:00:00', updated_at: '2026-10-03T12:01:00', delivery_state: 'success',
      dispatch_mode: 'normal', delivery_version: 3 };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
      http.get('/routing/fax-costs', () => HttpResponse.json({ costs: { [JOB]: {
        state: 'reported', summary: 'Telnyx charged $0.005 for this call.', reported_cost: [{ currency: 'USD', amount: '0.005' }],
        estimated_cost: [], route: 'sip', routes: ['sip'], route_reason: 'cheapest_delivered',
        route_explanation: 'The cheapest route per delivered fax to this number over the last 30 days.' } } })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+15550100001'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect((await within(dialog).findByTestId('job-route-reason')).textContent).toBe(
      'The cheapest route per delivered fax to this number over the last 30 days.');
  });
});
