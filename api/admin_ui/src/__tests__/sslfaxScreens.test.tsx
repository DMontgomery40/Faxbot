import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import RecipientFaxLimitsPanel from '../components/delivery/RecipientFaxLimits';
import Savings from '../components/delivery/Savings';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
const NUMBER = '+15555550199';

describe('SSL Fax on the Recipients and Costs screens', () => {
  it('says whether the fax machine takes faster pages and saves its own speed and error correction', async () => {
    const writes: unknown[] = [];
    server.use(
      http.get('/routing/destinations/:number/fax-limits', () => HttpResponse.json({
        number: NUMBER, accepts_sslfax: true, accepts_sslfax_at: '2026-10-04T12:00:00Z', max_rate: null, ecm: null,
        sslfax_sentence: 'This fax machine can take pages faster, so faxes to it are quicker.' })),
      http.put('/routing/destinations/:number/fax-limits', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push(body);
        return HttpResponse.json({ number: NUMBER, accepts_sslfax: true, accepts_sslfax_at: '2026-10-04T12:00:00Z',
          sslfax_sentence: 'This fax machine can take pages faster, so faxes to it are quicker.', ...body });
      }),
    );
    render(<RecipientFaxLimitsPanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-fax-limits');
    expect(panel.textContent).toContain('This fax machine can take pages faster, so faxes to it are quicker.');
    const save = within(panel).getByRole('button', { name: 'Save for this number' });
    expect(save).toHaveProperty('disabled', true);
    fireEvent.mouseDown(within(panel).getByLabelText('Highest speed'));
    fireEvent.click(await screen.findByRole('option', { name: '9,600 bits per second' }));
    fireEvent.click(save);
    expect(await screen.findByText('Saved for the next fax.')).toBeTruthy();
    expect(writes).toEqual([{ max_rate: 9600, ecm: null }]);
  });

  it('shows the faster-pages saving as an estimate', async () => {
    const part = { sentence: '', saved: [], estimate: true };
    server.use(http.get('/routing/savings', () => HttpResponse.json({
      days: 30, since: '2026-09-04T00:00:00', estimate: true, sentence: 'Each figure is an estimate.', total_saved: [],
      sending_together: { ...part, numbers: 0, calls: 0, faxes: 0, calls_saved: 0, priced_calls: 0 },
      direct_delivery: { ...part, faxes: 0, calls_avoided: 0, pages: 0, priced: 0, in_plan: 0, unpriced: 0 },
      case_packets: { ...part, counted_from: null, earlier_not_counted: false, counted_from_sentence: null, packets: 0,
        documents_left_out: 0, pages_not_resent: 0, pages_saved: 0, priced: 0, in_plan: 0, unpriced: 0 },
      sslfax: { ...part, faxes: 3, seconds_saved: 110, priced: 3, in_plan: 0, unpriced: 0, same_cost: 3,
        sentence: '3 faxes had their pages sent faster: about 2 minutes less on the phone. Your carrier charges '
          + 'whole minutes, so they cost the same.' },
    })));
    render(<Savings client={client()} />);
    const sslfax = await screen.findByTestId('savings-sslfax');
    await waitFor(() => expect(sslfax.textContent).toContain('3 faxes had their pages sent faster'));
    expect(within(sslfax).getByText('Estimate')).toBeTruthy();
  });
});
