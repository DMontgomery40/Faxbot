import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Recommendations from '../components/delivery/Recommendations';
import { server } from '../test/server';
import fixture from './opportunitiesFacts.json';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const path = '/routing/recommendations/facts';

describe('Opportunities includes missing facts', () => {
  it('shows the shared CLI fixture, evidence, agreement and effort without the scenario planner or an Apply action', async () => {
    server.use(http.get(path, () => HttpResponse.json(fixture.advice)));
    render(<Recommendations client={client()} focus="facts" />);
    const section = await screen.findByTestId('recommendations-facts');
    await within(section).findByText(fixture.advice.recipients[0].facts[0].confirm);
    expect(section.getAttribute('data-focused')).toBe('true');
    expect(section.textContent).toContain('Evidence period: last 90 days.');
    for (const row of fixture.advice.recipients[0].facts) {
      expect(section.textContent).toContain(row.sentence);
      expect(section.textContent).toContain(row.confirm);
      expect(section.textContent).toContain(row.step);
      expect(section.textContent).toContain(row.establish);
    }
    expect(section.textContent).toContain('Expected benefit: unknown.');
    expect(section.textContent).toContain('Cautious benefit: unknown.');
    expect(within(section).queryByRole('button', { name: /apply/i })).toBeNull();
    expect(screen.queryByTestId('recommendations-empty')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Setup planner' })).toBeNull();
  });

  it('does not claim no opportunities while facts are loading, and reports an empty facts section after loading', async () => {
    let resolve!: (value: Response) => void;
    server.use(http.get(path, () => new Promise<Response>((done) => { resolve = done; })));
    render(<Recommendations client={client()} focus="facts" />);
    await screen.findByText('Loading missing-fact advice…');
    expect(screen.queryByTestId('recommendations-empty')).toBeNull();
    await waitFor(() => expect(resolve).toBeTypeOf('function'));
    resolve(HttpResponse.json({ ...fixture.advice, recipients: [], state: 'none' }));
    await screen.findByTestId('recommendations-empty');
  });

  it.each([403, 503])('keeps a failed facts read visible (%s), with a working retry', async (status) => {
    const read = vi.fn().mockReturnValueOnce(new HttpResponse(null, { status }))
      .mockImplementation(() => HttpResponse.json(fixture.advice));
    server.use(http.get(path, read));
    render(<Recommendations client={client()} focus="facts" />);
    const section = await screen.findByTestId('recommendations-facts');
    await within(section).findByRole('alert');
    expect(screen.queryByTestId('recommendations-empty')).toBeNull();
    fireEvent.click(within(section).getByRole('button', { name: 'Refresh' }));
    await within(section).findByText(fixture.advice.recipients[0].facts[0].confirm);
    expect(section.textContent).not.toContain("couldn't work out");
    expect(section.textContent).not.toContain('do not have permission');
    expect(read).toHaveBeenCalledTimes(2);
  });
});
