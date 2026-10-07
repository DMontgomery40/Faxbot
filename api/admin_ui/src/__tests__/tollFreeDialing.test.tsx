// Toll-free dialing: Sent details say which approved number a fax dialed, Prices & plans list what calling a
// toll-free number costs on each route, and Savings reports the approved toll-free numbers on their own.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { TollFreeTerms } from '../api/deliveryTypes';
import JobsList from '../components/JobsList';
import TollFreePrices from '../components/delivery/TollFreePrices';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const JOB = 'd'.repeat(32);
const DIALED = 'Dialed 1-800-555-0100, the toll-free number Example Clinic approved on October 3, 2026.';

function sentFax(dialed: object | null) {
  const job = { id: JOB, to_number: '+12025550123', status: 'SUCCESS', backend: 'sip', pages: 2,
    created_at: '2026-10-07T12:00:00', updated_at: '2026-10-07T12:01:00', delivery_state: 'success',
    dispatch_mode: 'normal', delivery_version: 3 };
  server.use(
    http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
    http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
    http.get('/routing/fax-costs', () => HttpResponse.json({ costs: { [JOB]: {
      state: 'reported', summary: 'Telnyx charged $0.00 for this call.', reported_cost: [], estimated_cost: [],
      route: 'sip', routes: ['sip'], route_reason: 'cheapest',
      route_explanation: 'The cheapest route that works reliably for this number.', dialed } } })),
  );
}

describe('Sent details', () => {
  it('say which approved toll-free number the fax dialed', async () => {
    sentFax({ number: '+18005550100', display: '1-800-555-0100', toll_free: true, recipient_name: 'Example Clinic',
      approved_on: 'October 3, 2026', withdrawn_on: null, sentence: DIALED });
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+12025550123'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect((await within(dialog).findByTestId('job-dialed-number')).textContent).toBe(DIALED);
  });

  it('say nothing more when the fax dialed the number entered', async () => {
    sentFax(null);
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+12025550123'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    await within(dialog).findByTestId('job-route-reason');
    expect(within(dialog).queryByTestId('job-dialed-number')).toBeNull();
  });
});

describe('Prices & plans', () => {
  const telnyx: TollFreeTerms = {
    provider_id: 'sip', route: 'sip-telnyx', provider_name: 'Telnyx', label: 'Telnyx SIP trunk, calls to US toll-free numbers',
    reaches: 'yes', reach_text: 'Calls toll-free numbers.', price_text: 'Free.', pricing: 'own',
    caller_id_text: 'A number on your account, or one the carrier verified.', advertised_on: '2026-10-07',
    source_url: 'https://telnyx.com/pricing/elastic-sip',
  };

  it('list what calling a toll-free number costs on each route, with its source', () => {
    render(<TollFreePrices items={[telnyx]} />);
    const item = screen.getByTestId('toll-free-sip');
    expect(within(item).getByText('Telnyx')).toBeTruthy();
    expect(within(item).getByText('Price: Free.')).toBeTruthy();
    expect(within(item).getByText('Caller ID it needs: A number on your account, or one the carrier verified.')).toBeTruthy();
    expect(within(item).getByRole('link', { name: 'source' }).getAttribute('href')).toBe('https://telnyx.com/pricing/elastic-sip');
    // A date in words, never the stored form.
    expect(item.textContent).not.toContain('2026-10-07');
  });

  it('show nothing when no sending route publishes toll-free terms', () => {
    const { container } = render(<TollFreePrices items={[]} />);
    expect(container.textContent).toBe('');
  });
});
