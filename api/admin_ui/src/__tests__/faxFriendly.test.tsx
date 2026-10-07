import { describe, expect, it } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import FaxFriendlyRecommendation from '../components/delivery/FaxFriendlyRecommendation';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
const BASE = {
  enabled: false, label: 'Lighten shaded areas and remove specks on documents you send', measured_sentence: '',
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

  it('says what it saved once on, without counting as a recommendation', async () => {
    const counts: Array<number | null> = [];
    server.use(http.get('/routing/recommendations/fax-friendly', () => HttpResponse.json({
      ...BASE, enabled: true, faxes_changed: 1, seconds_saved: 50,
      sentence: 'Shaded areas were lightened and specks removed on 1 fax in the last 30 days: an estimated '
        + '50 seconds less on the line.',
    })));
    render(<FaxFriendlyRecommendation client={client()} onCount={(count) => counts.push(count)} />);
    expect((await screen.findByTestId('fax-friendly-sentence')).textContent).toContain('on 1 fax in the last 30 days');
    expect(screen.queryByText('Worth turning on')).toBeNull();
    expect(screen.queryByTestId('fax-friendly-action')).toBeNull();
    expect(counts).toEqual([0]);
  });

  it('shows nothing when there is nothing to say', async () => {
    const counts: Array<number | null> = [];
    server.use(http.get('/routing/recommendations/fax-friendly', () => HttpResponse.json(BASE)));
    render(<FaxFriendlyRecommendation client={client()} onCount={(count) => counts.push(count)} />);
    await waitFor(() => expect(counts).toEqual([0]));
    expect(screen.queryByTestId('fax-friendly-recommendation')).toBeNull();
  });
});
