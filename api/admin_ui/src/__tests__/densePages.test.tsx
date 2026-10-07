import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import { RecipientPagesPanel, RoutePagesPanel } from '../components/delivery/PagesSettings';
import Savings from '../components/delivery/Savings';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
const NUMBER = '+15555550199';
const VIEW = {
  number: NUMBER, page_limit: 'unlimited', learned: true, learned_at: '2026-10-07T09:00:00Z', ecm: false,
  packing: 'allow', trim_blank: null, trim_blank_default: true,
  capability_sentence: 'This fax machine takes pages of unlimited length.',
  ecm_sentence: 'This fax machine has no error correction, so every line of a page takes time.',
};

describe('Dense pages on the Recipients, Delivery routes and Savings screens', () => {
  it('shows how long a page the machine takes and saves pages per sheet and blank space for one number', async () => {
    const writes: unknown[] = [];
    server.use(
      http.get('/routing/destinations/:number/pages', () => HttpResponse.json(VIEW)),
      http.put('/routing/destinations/:number/pages', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push(body);
        return HttpResponse.json({ ...VIEW, ...body });
      }),
    );
    render(<RecipientPagesPanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-pages');
    expect(panel.textContent).toContain('This fax machine takes pages of unlimited length. Learned on');
    expect(panel.textContent).toContain('This fax machine has no error correction');
    const save = within(panel).getByRole('button', { name: 'Save for this number' });
    expect(save).toHaveProperty('disabled', true);
    fireEvent.mouseDown(within(panel).getByLabelText('Pages per sheet'));
    fireEvent.click(await screen.findByRole('option', { name: 'Never' }));
    fireEvent.mouseDown(within(panel).getByLabelText('Blank space at the bottom of pages'));
    fireEvent.click(await screen.findByRole('option', { name: 'Off' }));
    fireEvent.click(save);
    expect(await screen.findByText('Saved for the next fax.')).toBeTruthy();
    expect(writes).toEqual([{ packing: 'never', trim_blank: false }]);
  });

  it('turns long pages on for a cloud route and keeps a route that fetches its own document off', async () => {
    const routes = [
      { route: 'sip', label: 'Telnyx', long_pages: true, long_pages_chosen: false, long_pages_possible: true,
        trim_blank: true, sentence: 'Faxbot puts several pages on one long page when the receiving machine takes long pages and it saves pages or time.' },
      { route: 'sinch', label: 'Sinch', long_pages: false, long_pages_chosen: false, long_pages_possible: true,
        trim_blank: null, sentence: 'Off until you check that Sinch sends long pages without shrinking them.' },
      { route: 'phaxio', label: 'Phaxio', long_pages: false, long_pages_chosen: false, long_pages_possible: false,
        trim_blank: null, sentence: 'Phaxio fetches the document from Faxbot itself, so long pages cannot be sent through it.' },
    ];
    const writes: unknown[] = [];
    server.use(
      http.get('/routing/page-routes', () => HttpResponse.json({ routes })),
      http.put('/routing/page-routes/:route', async ({ request, params }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push({ route: params.route, ...body });
        return HttpResponse.json({ ...routes.find((item) => item.route === params.route), ...body });
      }),
    );
    render(<RoutePagesPanel client={client()} canWrite routes={['sip', 'sinch', 'phaxio']} />);
    const panel = await screen.findByTestId('route-pages');
    const phaxio = within(panel).getByLabelText('Phaxio: several pages on one long page');
    expect(phaxio).toHaveProperty('disabled', true);
    fireEvent.click(within(panel).getByLabelText('Sinch: several pages on one long page'));
    await waitFor(() => expect(writes).toEqual([{ route: 'sinch', long_pages: true }]));
    fireEvent.click(within(panel).getByLabelText(
      'Blank space at the bottom of pages: leave it out for machines without error correction'));
    await waitFor(() => expect(writes).toContainEqual({ route: 'sip', trim_blank: false }));
  });

  it('shows the pages saved by packing as an estimate', async () => {
    const part = { sentence: '', saved: [], estimate: true };
    server.use(http.get('/routing/savings', () => HttpResponse.json({
      days: 30, since: '2026-09-07T00:00:00', estimate: true, sentence: 'Each figure is an estimate.', total_saved: [],
      sending_together: { ...part, numbers: 0, calls: 0, faxes: 0, calls_saved: 0, priced_calls: 0 },
      direct_delivery: { ...part, faxes: 0, calls_avoided: 0, pages: 0, priced: 0, in_plan: 0, unpriced: 0 },
      case_packets: { ...part, counted_from: null, earlier_not_counted: false, counted_from_sentence: null, packets: 0,
        documents_left_out: 0, pages_not_resent: 0, pages_saved: 0, priced: 0, in_plan: 0, unpriced: 0 },
      packing: { ...part, faxes: 1, pages_saved: 3, trimmed_pages: 0, seconds_saved: 9, priced: 1, in_plan: 0,
        plan_pages: 0, unpriced: 0, sentence: '3 pages saved by packing on 1 fax, saving about $0.135.' },
    })));
    render(<Savings client={client()} />);
    const packing = await screen.findByTestId('savings-packing');
    await waitFor(() => expect(packing.textContent).toContain('3 pages saved by packing on 1 fax'));
    expect(within(packing).getByText('Pages saved by packing')).toBeTruthy();
    expect(within(packing).getByText('Estimate')).toBeTruthy();
  });
});
