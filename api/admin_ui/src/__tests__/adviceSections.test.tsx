import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Recommendations, { NO_RECOMMENDATIONS } from '../components/delivery/Recommendations';
import ReceivingRecommendations, { STILL_PUBLISHED } from '../components/delivery/ReceivingRecommendations';
import { newFaxMarkerAdvice, newReceivingAdvice, server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const usd = (amount: string) => ({ currency: 'USD', amount });

describe('Costs → Recommendations: advice from history', () => {
  it('says where each section stands when there is nothing to suggest yet', async () => {
    render(<Recommendations client={client()} />);
    expect((await screen.findByTestId('recommendations-empty')).textContent).toMatch(NO_RECOMMENDATIONS);
    expect((await screen.findByTestId('fax-marker-sentence')).textContent).toMatch(
      'Faxbot placed no calls over your carrier line in the last 90 days, so there is nothing to compare yet.');
    expect(screen.getByText('Mark calls as fax is on, and Faxbot leaves it on: this comparison never changes a setting.'))
      .toBeTruthy();
    expect((await screen.findByTestId('billing-steps-sentence')).textContent).toMatch(
      'Faxbot has no carrier line set up, so there are no calls to measure.');
    expect((await screen.findByTestId('partners-sentence')).textContent).toMatch(/^Nothing to suggest/);
    expect((await screen.findByTestId('toll-free-sentence')).textContent).toMatch(/the recipient pays for those calls\.$/);
  });

  it('compares marked and unmarked calls in a table once there are enough of each', async () => {
    const side = (calls: number, delivered: number, t38: number, cost: string) => ({
      ...newFaxMarkerAdvice().marked, calls, delivered, delivered_percent: delivered, t38_percent: t38,
      seconds_per_page: 20, cost_text: cost, cost_per_delivered: usd('0.005') });
    server.use(http.get('/routing/recommendations/fax-marker', () => HttpResponse.json({
      ...newFaxMarkerAdvice(), state: 'compared', enough: true,
      sentence: 'Marked as fax: 100% delivered. Not marked: 80% delivered. Calls marked as fax delivered more often.',
      caveat: 'The two groups are calls from different days, not a controlled test, so other changes may explain part '
        + 'of any difference.',
      marked: side(10, 100, 90, '$0.005'), not_marked: side(10, 80, 50, 'About $0.0063') })));
    render(<Recommendations client={client()} />);
    const section = await screen.findByTestId('advice-fax-marker');
    const table = await within(section).findByRole('table', { name: 'Calls marked as fax and calls not marked' });
    expect(within(table).getByText('Used fax over IP (T.38)')).toBeTruthy();
    expect(within(table).getByText('90%')).toBeTruthy();
    expect(within(table).getByText('About $0.0063')).toBeTruthy();
    expect(within(section).getByText(/not a controlled test/)).toBeTruthy();
    // A comparison is something to read, so the page no longer says there is nothing to suggest.
    await waitFor(() => expect(screen.queryByTestId('recommendations-empty')).toBeNull());
  });

  it('lists numbers whose calls end just past a billed minute', async () => {
    server.use(http.get('/routing/recommendations/billing-steps', () => HttpResponse.json({
      days: 30, estimate: true, carrier: 'Telnyx', state: 'near', min_calls: 3, numbers_total: 1, calls_near: 3,
      saving: usd('0.015'),
      sentence: 'Calls to one number often end just past a billed minute: one page less or a faster mode would have '
        + 'saved a minute on 3 calls, about $0.015 in the last 30 days (estimate).',
      step: { seconds: 60, minimum_seconds: 0, near_seconds: 10, price: usd('0.005'), price_text: '$0.005',
        source_url: 'https://telnyx.com/pricing/elastic-sip', read_on: '2026-10-05' },
      numbers: [{ number: '+12025550123', display_name: 'Synthetic Clinic', calls: 5, calls_near: 3,
        seconds_past: { least: 1, most: 10 }, saving: usd('0.015'), sentence: 'Calls to this number end 1–10 s past a '
          + 'billed minute on 3 of 5 calls.' }] })));
    render(<Recommendations client={client()} />);
    const section = await screen.findByTestId('advice-billing-steps');
    const table = await within(section).findByRole('table', { name: 'Calls that end just past a billed step' });
    expect(within(table).getByText('Synthetic Clinic')).toBeTruthy();
    expect(within(table).getByText('1–10 s')).toBeTruthy();
    expect(within(table).getByText('$0.015')).toBeTruthy();
    expect(within(section).getByText(/Telnyx bills in 60-second steps of \$0\.005, from your price list dated/)).toBeTruthy();
  });

  it('suggests partner candidates with a way to enroll them, and unknown cost is never $0', async () => {
    const navigate = vi.fn();
    const candidate = (number: string, monthly: { currency: string; amount: string } | null, text: string) => ({
      number, display_name: null, faxes: 5, delivered: 5, average_pages: 12, monthly_cost: monthly, cost_text: text,
      estimate: true, unpriced_faxes: monthly ? 0 : 3, sentence: `Sentence for ${number}.`,
      link: 'recipients/partners', link_label: 'Enroll as direct partner' });
    server.use(http.get('/routing/recommendations/partners', () => HttpResponse.json({
      days: 30, min_faxes: 3, estimate: true, state: 'candidates', sentence: 'These numbers cost the most to fax again '
        + 'and again.', items_total: 2, link: 'recipients/partners',
      items: [candidate('+12025550123', usd('0.09'), 'About $0.09'), candidate('+13035550177', null, 'Unknown')] })));
    render(<Recommendations client={client()} onNavigate={navigate} />);
    const section = await screen.findByTestId('advice-partners');
    const [first, second] = await within(section).findAllByTestId('partner-candidate');
    expect(within(first).getByText('About $0.09 a month')).toBeTruthy();
    expect(within(second).getByText('Unknown')).toBeTruthy();
    expect(within(second).queryByText(/\$0\.00/)).toBeNull();
    fireEvent.click(within(first).getByRole('button', { name: 'Enroll as direct partner' }));
    expect(navigate).toHaveBeenCalledWith('recipients/partners');
  });

  it('shows each toll-free number on file and whether it is approved', async () => {
    const row = (approved: boolean) => ({ id: approved ? 'a' : 'b', number: approved ? '+12025550123' : '+12025550140',
      alternate_number: '+18005550100', alternate_display: '+1 800-555-0100', action: approved ? 'approved' : 'noted',
      approved_by: approved ? 'Dana' : null, approved_on: approved ? '2026-10-03' : null, evidence: null,
      recorded_by_name: 'Owner', recorded_at: '2026-10-07T15:00:00', display_name: approved ? 'Synthetic Clinic' : null,
      approved, spend: null, sentence: approved ? 'Dana agreed on 3 October 2026. With this approval, Faxbot sends '
        + 'faxes for Synthetic Clinic to +1 800-555-0100, and the recipient pays for those calls.' : 'On file, not approved.' });
    server.use(http.get('/routing/recommendations/toll-free', () => HttpResponse.json({ state: 'on_file', days: 30,
      sentence: '2 recipients have a toll-free fax number on file, 1 approved.', items: [row(true), row(false)] })));
    render(<Recommendations client={client()} />);
    const section = await screen.findByTestId('advice-toll-free');
    const [approved, noted] = await within(section).findAllByTestId('toll-free-item');
    expect(within(approved).getByText('Approved')).toBeTruthy();
    expect(within(approved).getByText(/the recipient pays for those calls/)).toBeTruthy();
    expect(within(noted).getByText('Not approved yet')).toBeTruthy();
  });

  it('asks whether a quiet number is still published, and lists HumbleFax and eFax numbers', async () => {
    const advice = newReceivingAdvice();
    server.use(http.get('/routing/recommendations/receiving', () => HttpResponse.json({
      ...advice,
      quiet_numbers: { state: 'quiet', sentence: 'One of your Telnyx numbers had 2 calls or fewer in the last 30 days.',
        numbers: [{ number: '+17205550199', received: 0, sent: 0, monthly_rental: [usd('1.00')],
          question: 'Is +1 720-555-0199 still printed on your letterhead, forms or website, or listed anywhere? If it '
            + 'is, keep it.' }], monthly_total: [usd('1.00')] },
      provider_numbers: { state: 'quiet', most_faxes: 2, sentence: '1 fax service number had 2 faxes or fewer in the '
        + 'last 30 days. Before you give one up, check that it is not still printed or published anywhere.',
        numbers: [{ provider: 'humblefax', name: 'HumbleFax', number: '+13035550197', received: 1, sent: 0,
          enough_history: true, quiet: true, plan_fee: [usd('10.00')],
          question: 'Is +1 303-555-0197 still printed on your letterhead, forms or website, or listed anywhere? If it '
            + 'is, keep it.',
          sentence: 'HumbleFax number +1 303-555-0197 had 1 fax in the last 30 days, 1 received and 0 sent.' }] },
    })));
    render(<ReceivingRecommendations client={client()} />);
    const services = await screen.findByTestId('receiving-service-numbers');
    expect(within(services).getByRole('table', { name: 'Fax service numbers' })).toBeTruthy();
    expect(within(services).getByText('$10.00')).toBeTruthy();
    expect(within(services).getByText(/had 1 fax in the last 30 days, 1 received and 0 sent\. Is \+1 303-555-0197 still printed/))
      .toBeTruthy();
    expect((screen.getByTestId('receiving-quiet-question')).textContent).toMatch(STILL_PUBLISHED);
  });
});
