import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { PlanAllocationPlan } from '../api/deliveryTypes';
import PlanAllocation from '../components/delivery/PlanAllocation';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

// Synthetic: eFax has 100 included pages left; a 60-page fax goes by Telnyx so a 100-page fax gets them.
function efax(changes: Partial<PlanAllocationPlan> = {}): PlanAllocationPlan {
  return {
    route: 'efax', name: 'eFax', unit: 'pages', room: 100, on_their_way: 0, renews_on: '2026-11-01', reserve: 0,
    reserve_sentence: 'Faxbot keeps nothing back: your earlier faxes do not show dearer ones coming before 1 November.',
    sentence: 'eFax has 100 included pages left until 1 November: 100 go to 1 waiting fax.',
    left_sentence: 'eFax has used 100 of the 200 pages your plan includes since 1 October; 100 are left until 1 November.',
    saving: [{ currency: 'USD', amount: '49.50' }],
    saving_sentence: 'Sharing the pages this way saves about $49.50 against giving them to the waiting faxes in turn (estimate).',
    bound_sentence: null,
    faxes: [
      { job_id: 'synthetic-small', to: '+12025550141', pages: 60, units: 60, queued_at: '2026-10-20T15:00:00',
        urgent: false, send_by: null, outcome: 'other', route: 'sip', route_label: 'Telnyx',
        cost: [{ currency: 'USD', amount: '0.50' }],
        sentence: 'Goes by Telnyx for about $0.50, so the plan\'s pages go where they save more.' },
      { job_id: 'synthetic-large', to: '+12025550142', pages: 100, units: 100, queued_at: '2026-10-20T15:01:00',
        urgent: false, send_by: null, outcome: 'plan', route: 'efax', route_label: 'eFax', cost: [],
        sentence: 'Gets 100 pages of the plan; by Sinch it would cost about $50.' },
    ],
    ...changes,
  };
}

describe('Costs → Prices & plans → Who gets your plans’ last pages', () => {
  it('says there is nothing to share out when no plan is limited', async () => {
    render(<PlanAllocation client={client()} />);
    expect((await screen.findByTestId('plan-allocation-empty')).textContent).toBe(
      'None of your plans has a limited allowance or a normal-use budget this month, so there is nothing to share out.');
  });

  it('shows what is left, who gets it, who goes another way and what is kept for later', async () => {
    server.use(http.get('/routing/plans/allocation', () => HttpResponse.json({
      plans: [efax({ reserve: 40, reserve_sentence: 'Faxbot keeps 40 pages for faxes like the ones you usually send before 1 November: in 4 earlier stretches of 12 days, faxes like them would have saved at least about $8 with them (estimate).' })],
      estimate: true, empty_sentence: null })));
    render(<PlanAllocation client={client()} />);
    const plan = await screen.findByTestId('plan-allocation-efax');
    expect(within(plan).getByText('eFax has 100 included pages left until 1 November: 100 go to 1 waiting fax.')).toBeTruthy();
    const rows = within(plan).getAllByRole('row');
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByText('Goes another way')).toBeTruthy();
    expect(within(rows[1]).getByText('Goes by Telnyx for about $0.50, so the plan\'s pages go where they save more.')).toBeTruthy();
    expect(within(rows[2]).getByText('Gets the plan')).toBeTruthy();
    expect(within(plan).getByText(/saves about \$49\.50 against giving them to the waiting faxes in turn/)).toBeTruthy();
    expect(within(plan).getByText(/Faxbot keeps 40 pages for faxes like the ones you usually send/)).toBeTruthy();
    // No internal identifiers or raw timestamps on the screen.
    expect(plan.textContent).not.toContain('synthetic-small');
    expect(plan.textContent).not.toContain('2026-10-20T15:00:00');
  });
});
