// Recipients → Details → NTT Data Connect, and the Data Connect sentence in Sent.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList from '../components/JobsList';
import { RecipientDataConnect, type DataConnectView } from '../components/delivery/RecipientDataConnect';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+81355550123';
const ADVICE = ['Data Connect works only between Data Connect-capable devices at both ends.',
  'Residential Hikari Denwa carries fax as voice, not as T.38.'];

const view: DataConnectView = {
  number: NUMBER, capable: false, known: false, source: null, learned_on: null,
  sentence: "Faxbot does not know whether this number's fax machine takes Data Connect calls; it calls it as an ordinary fax call.",
  trunks: [{ account: 'sip', label: 'NTT Hikari Denwa', enabled: true, reason: null }], advice: ADVICE,
  price: "A Data Connect call, because this number's fax machine takes them: NTT charges ¥1.1 per started 30 seconds.",
  not_run: 'Faxbot has not yet run Data Connect against NTT.',
};

describe('NTT Data Connect on Recipients → Details', () => {
  it("records the machine's fact, turns the trunk off and keeps NTT's limits in view", async () => {
    const sent: { path: string; body: unknown }[] = [];
    server.use(
      http.get(`/routing/destinations/${encodeURIComponent(NUMBER)}/data-connect`, () => HttpResponse.json(view)),
      http.put(`/routing/destinations/${encodeURIComponent(NUMBER)}/data-connect`, async ({ request }) => {
        sent.push({ path: 'number', body: await request.json() });
        return HttpResponse.json({ ...view, capable: true, known: true, source: 'admin',
          sentence: "You recorded that this number's fax machine takes Data Connect calls." });
      }),
      http.put('/routing/data-connect/trunks/sip', async ({ request }) => {
        sent.push({ path: 'trunk', body: await request.json() });
        return HttpResponse.json({ account: 'sip', enabled: false, reason: null });
      }),
    );
    render(<RecipientDataConnect client={client()} number={NUMBER} canWrite />);
    const region = await screen.findByRole('region', { name: 'NTT Data Connect' });
    expect(within(region).getByTestId('data-connect-sentence').textContent).toBe(view.sentence);
    expect(within(region).getByTestId('data-connect-advice').textContent).toBe(ADVICE.join(''));
    expect(within(region).getByText(view.not_run)).toBeTruthy();
    fireEvent.click(within(region).getByRole('checkbox', { name: "This number's fax machine takes Data Connect calls" }));
    await waitFor(() => expect(within(region).getByTestId('data-connect-sentence').textContent).toBe(
      "You recorded that this number's fax machine takes Data Connect calls."));
    fireEvent.click(within(region).getByRole('checkbox', { name: 'Data Connect calls over NTT Hikari Denwa' }));
    await waitFor(() => expect((within(region).getByRole('checkbox', {
      name: 'Data Connect calls over NTT Hikari Denwa' }) as HTMLInputElement).checked).toBe(false));
    expect(sent).toEqual([{ path: 'number', body: { capable: true } }, { path: 'trunk', body: { enabled: false } }]);
  });

  it('shows what Faxbot learned from a refused call, read-only without permission', async () => {
    server.use(http.get(`/routing/destinations/${encodeURIComponent(NUMBER)}/data-connect`, () => HttpResponse.json({
      ...view, known: true, source: 'learned', learned_on: '2026-10-09',
      sentence: "This number's machine or NTT's network refused a Data Connect call." })));
    render(<RecipientDataConnect client={client()} number={NUMBER} canWrite={false} />);
    const region = await screen.findByRole('region', { name: 'NTT Data Connect' });
    expect(within(region).getByTestId('data-connect-sentence').textContent).toContain('refused a Data Connect call.');
    expect((within(region).getByRole('checkbox', { name: "This number's fax machine takes Data Connect calls" }) as HTMLInputElement).disabled).toBe(true);
  });

  it('stays out of the way without an NTT trunk', async () => {
    server.use(http.get(`/routing/destinations/${encodeURIComponent(NUMBER)}/data-connect`, () => HttpResponse.json({
      ...view, trunks: [] })));
    const { container } = render(<RecipientDataConnect client={client()} number={NUMBER} canWrite />);
    await waitFor(() => expect(container.textContent).toBe(''));
  });

  it("says when the recipient's machine refused and Faxbot called again", async () => {
    const JOB = 'd'.repeat(32);
    const job = { id: JOB, to_number: NUMBER, status: 'SUCCESS', backend: 'sip', pages: 2, created_at: '2026-10-10T03:00:00',
      updated_at: '2026-10-10T03:02:00', delivery_state: 'succeeded', dispatch_mode: 'normal', delivery_version: 3 };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json({ ...job,
        data_connect: "The recipient's machine doesn't take Data Connect; Faxbot called again as an ordinary fax call." })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText(NUMBER));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect((await within(dialog).findByTestId('job-data-connect')).textContent).toBe(
      "The recipient's machine doesn't take Data Connect; Faxbot called again as an ordinary fax call.");
  });
});
