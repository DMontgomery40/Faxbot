import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import FaxFriendlyRecommendation from '../components/delivery/FaxFriendlyRecommendation';
import { RecipientPagesPanel } from '../components/delivery/PagesSettings';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
const BASE = {
  choice: 'never', label: 'Lighten shaded areas and remove specks on documents you send', measured_sentence: '',
  days: 30, recommend: false, faxes_checked: 0, faxes_changed: 0, seconds_saved: 0, sentence: null, action: null,
};

describe('Costs, Recommendations: shaded areas and specks', () => {
  it('recommends turning it on when recent faxes would have saved time, as an estimate', async () => {
    const counts: Array<number | null> = [];
    server.use(http.get('/routing/recommendations/fax-friendly', () => HttpResponse.json({
      ...BASE, recommend: true, faxes_checked: 10, faxes_changed: 3, seconds_saved: 144,
      sentence: 'Your last 10 faxes would have taken an estimated 2 minutes less on the line with shaded areas '
        + 'lightened and specks removed; 3 of them have shaded areas or specks.',
      action: 'Turn on "Lighten shaded areas and remove specks on documents you send" under Providers, In use, '
        + 'Delivery routes. Shaded areas then print white and photographs lose their lightest parts.',
    })));
    render(<FaxFriendlyRecommendation client={client()} onCount={(count) => counts.push(count)} />);
    const sentence = await screen.findByTestId('fax-friendly-sentence');
    expect(sentence.textContent).toContain('an estimated 2 minutes less on the line');
    expect(screen.getByTestId('fax-friendly-action').textContent).toContain('under Providers, In use, Delivery routes');
    expect(screen.getByText('Worth turning on')).toBeTruthy();
    expect(screen.getByText('Estimate')).toBeTruthy();
    expect(counts).toEqual([1]);
  });

  it('explains when the providers used charge per page, without counting as a recommendation', async () => {
    const counts: Array<number | null> = [];
    server.use(http.get('/routing/recommendations/fax-friendly', () => HttpResponse.json({
      ...BASE, faxes_checked: 2,
      sentence: 'Your last 2 faxes went by providers that charge per page, to machines with error correction, so '
        + 'lightening shaded areas would have saved nothing.',
    })));
    render(<FaxFriendlyRecommendation client={client()} onCount={(count) => counts.push(count)} />);
    expect((await screen.findByTestId('fax-friendly-sentence')).textContent).toContain('charge per page');
    expect(screen.queryByText('Worth turning on')).toBeNull();
    expect(screen.queryByTestId('fax-friendly-action')).toBeNull();
    expect(counts).toEqual([0]);
  });

  it('shows nothing while shaded areas are lightened where it saves time', async () => {
    const counts: Array<number | null> = [];
    server.use(http.get('/routing/recommendations/fax-friendly', () => HttpResponse.json({
      ...BASE, choice: 'where_it_saves' })));
    render(<FaxFriendlyRecommendation client={client()} onCount={(count) => counts.push(count)} />);
    await waitFor(() => expect(counts).toEqual([0]));
    expect(screen.queryByTestId('fax-friendly-recommendation')).toBeNull();
  });
});

describe('Recipients, Details: lightening shaded areas for one recipient', () => {
  it('shows the setting for all faxes and saves never for this recipient', async () => {
    const view = {
      number: '+15555550199', page_limit: 'a4', learned: false, learned_at: null, ecm: null, packing: 'allow',
      trim_blank: null, trim_blank_default: true, shading: null, shading_default: 'where_it_saves',
      capability_sentence: 'Faxbot does not know yet how long a page this fax machine takes.', ecm_sentence: null,
    };
    const writes: unknown[] = [];
    server.use(
      http.get('/routing/destinations/:number/pages', () => HttpResponse.json(view)),
      http.put('/routing/destinations/:number/pages', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push(body);
        return HttpResponse.json({ ...view, ...body });
      }),
    );
    render(<RecipientPagesPanel client={client()} number="+15555550199" canWrite />);
    const panel = await screen.findByTestId('recipient-pages');
    fireEvent.mouseDown(within(panel).getByLabelText('Lighten shaded areas for this recipient'));
    expect(await screen.findByRole('option', { name: 'As set for all faxes (where it saves time)' })).toBeTruthy();
    fireEvent.click(await screen.findByRole('option', { name: 'Never' }));
    fireEvent.click(within(panel).getByRole('button', { name: 'Save for this number' }));
    expect(await screen.findByText('Saved for the next fax.')).toBeTruthy();
    expect(writes).toEqual([{ packing: 'allow', trim_blank: null, shading: 'never' }]);
  });
});
