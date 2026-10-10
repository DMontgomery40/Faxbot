// Costs → Recommendations → Fax server renewal (N20, N24). Synthetic data only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import FaxServerRenewal, { type ChannelsView, type RenewalsView } from '../components/delivery/FaxServerRenewal';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const CHANNELS: ChannelsView = {
  sentence: null, note: 'Advice only: Faxbot never changes a licence or contacts a vendor.',
  systems: [{ system: 'RightFax at HQ', own: false, source: 'RightFax DocTransport audit log',
    report: { calls: 3, peak: 3, p99: 3, sentence: '3 calls over 1 day: at most 3 at once. So 5 of your 8 licensed channels never carried a call.',
      profile: [{ hour: 9, p99: 3, peak: 3 }, { hour: 10, p99: 0, peak: 0 }] },
    imports: [{ id: 'import-1', format_label: 'RightFax DocTransport audit log', file_name: 'audit.log', calls: 3 }] }],
};
const RENEWALS: RenewalsView = {
  sentence: null, help: { routes: 'A CSV with the columns number, user, email and cover sheet.' },
  note: 'Faxbot only advises: it never cancels a renewal, changes a licence or contacts a vendor.',
  pages: [{ system: 'RightFax at HQ', renewal: { state: 'review', source_url: null },
    sentences: ['RightFax 22.2 renews on 31 May 2027 for $26,756.71: 82 days left to decide.',
      "At this renewal's average of $3,344.59 a channel, those 5 channels are $16,722.94 of it, if it is priced by channel.",
      '2 of its 3 numbers are not on Faxbot yet, for 1 user.'],
    left: { numbers: [{ number: '+13035550110', display: '+1 303-555-0110', user_name: 'Grace', user_email: 'grace@example.com' }] },
    reference: [{ id: 'cuyahoga-rightfax', sentence: 'Cuyahoga County, Ohio approved up to $26,756.71.', source: 'https://example.org/agenda.pdf',
      label: 'Cuyahoga County Board of Control agenda, 28 April 2025', read_on: '2026-10-08' }] }],
};

describe('Fax server renewal', () => {
  it('reads the one page, the busy hours and the published figures beside it', async () => {
    server.use(http.get('/routing/renewals', () => HttpResponse.json(RENEWALS)),
      http.get('/routing/channels', () => HttpResponse.json(CHANNELS)));
    let counted: number | null = null;
    render(<FaxServerRenewal client={client()} canWrite={false} onCount={(count) => { counted = count; }} />);
    const section = await screen.findByTestId('fax-server-renewal');
    expect(section.textContent).toContain('82 days left to decide.');
    expect(section.textContent).toContain('those 5 channels are $16,722.94 of it');
    expect(section.textContent).toContain('Still to move: +1 303-555-0110 (Grace).');
    expect(section.textContent).toContain('For comparison: Cuyahoga County, Ohio approved up to $26,756.71.');
    const hours = within(section).getByRole('table', { name: 'RightFax at HQ by hour of the day' });
    expect(within(hours).getAllByRole('row')).toHaveLength(2); // the header and the one busy hour
    expect(screen.queryByRole('button', { name: 'Enter a renewal' })).toBeNull();
    await waitFor(() => expect(counted).toBe(2));
  });

  it('saves a renewal with its parallel numbers and leaves an imported file out', async () => {
    const sent: unknown[] = [];
    const removed: string[] = [];
    server.use(http.get('/routing/renewals', () => HttpResponse.json({ ...RENEWALS, pages: [] })),
      http.get('/routing/channels', () => HttpResponse.json(CHANNELS)),
      http.put('/routing/renewals', async ({ request }) => { sent.push(await request.json()); return HttpResponse.json(RENEWALS); }),
      http.delete('/routing/channels/imports/:id', ({ params }) => {
        removed.push(String(params.id));
        return HttpResponse.json({ ...CHANNELS, systems: [] });
      }));
    render(<FaxServerRenewal client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Enter a renewal' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Fax server'), { target: { value: 'RightFax at HQ' } });
    fireEvent.change(within(dialog).getByLabelText('Renews on'), { target: { value: '2027-05-31' } });
    fireEvent.change(within(dialog).getByLabelText('Renewal amount'), { target: { value: '26756.71' } });
    fireEvent.change(within(dialog).getByLabelText('Channels it licenses'), { target: { value: '8' } });
    fireEvent.change(within(dialog).getByLabelText('Numbers Faxbot receives on beside it'), { target: { value: '+13035550100, +13035550101' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(sent).toEqual([{ system: 'RightFax at HQ', product: '', renews_on: '2027-05-31', amount: '26756.71',
      currency: 'USD', licensed_channels: 8, source_url: null, parallel_numbers: ['+13035550100', '+13035550101'],
      parallel_since: null }]));
    expect((await screen.findByTestId('fax-server-renewal')).textContent).toContain('Renewal saved.');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Leave audit.log out' }));
    await waitFor(() => expect(removed).toEqual(['import-1']));
  });

  it('imports call records for a named system', async () => {
    const fields: Array<Record<string, string>> = [];
    server.use(http.post('/routing/channels/files', async ({ request }) => {
      const form = await request.formData();
      fields.push(Object.fromEntries([...form.entries()].filter(([key]) => key !== 'file').map(([key, value]) => [key, String(value)])));
      return HttpResponse.json({ ...CHANNELS, imported: 3, skipped_count: 1 });
    }));
    render(<FaxServerRenewal client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Import call records' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Fax server'), { target: { value: 'RightFax at HQ' } });
    fireEvent.change(within(dialog).getByLabelText('Call records file'), { target: { files: [new File(['S,09/01/2026,09:00,0,120'], 'audit.log')] } });
    fireEvent.change(within(dialog).getByLabelText('Channels it is licensed for'), { target: { value: '8' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(fields).toEqual([{ system: 'RightFax at HQ', licensed: '8' }]));
    expect((await screen.findByTestId('fax-server-renewal')).textContent).toContain('Imported 3 calls. 1 line was not read.');
  });
});
