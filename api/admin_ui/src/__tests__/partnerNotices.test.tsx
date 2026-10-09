import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { DirectNotice } from '../api/deliveryTypes';
import DeliveryRoutes from '../components/DeliveryRoutes';
import { ReceivedNotice } from '../components/delivery/PartnerActivity';
import { server } from '../test/server';

type Recorded = { method: string; path: string; body: unknown };

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const partner = {
  id: 'peer-1', organization: 'County Clinic', fax_number: '+15550100002', endpoint: 'https://clinic.example',
  state: 'verified', status: 'Verified; faxes to this number are delivered directly.', code_sent: false,
  code_expires_at: null, verified_at: '2026-10-07T01:00:00', expires_at: null, version: 3, notice_fax: false,
  notice_fax_text: null, certificate_changed: false, certificate_text: null,
};

const waiting: DirectNotice = {
  id: 'n-1', direction: 'inbound', partner: 'County Clinic', state: 'waiting',
  status: 'The document from County Clinic arrived directly and waits for its notice fax.',
  code: '1234 5678 9012 3456 7890', matched_by: null, fax_id: null, original_fax_id: null, paired_by: null,
  created_at: '2026-10-07T15:00:00', paired_at: null,
};

function handlers(calls: Recorded[], notices: DirectNotice[] = [waiting]) {
  const record = async (request: Request) => {
    const text = await request.text();
    calls.push({ method: request.method, path: new URL(request.url).pathname, body: text ? JSON.parse(text) : null });
  };
  server.use(
    http.get('/direct/peers', () => HttpResponse.json({ peers: [partner] })),
    http.get('/direct/notices', () => HttpResponse.json({ notices })),
    http.get('/direct/transfers', () => HttpResponse.json({ transfers: [{
      message_id: 'a'.repeat(32), direction: 'outbound', partner: 'County Clinic', state: 'open',
      status: 'Sending in pieces: 12 of 50 pieces confirmed.', pieces: 50, confirmed: 12, size_bytes: 52428816,
      created_at: '2026-10-07T15:00:00', finished_at: null }] })),
    http.get('/direct/repairs', () => HttpResponse.json({ repairs: [{
      id: 'r-1', direction: 'outbound', partner: 'County Clinic', state: 'completed',
      status: 'Completed: the missing pages went directly and County Clinic holds the whole fax. County Clinic held '
        + 'the first 6 of 10 pages from the call; only pages 7 to 10 went directly.',
      fax_id: 'f'.repeat(32), pages_held: 6, total_pages: 10, error_correction: true,
      created_at: '2026-10-07T14:00:00' }] })),
    http.get('/direct/notices/n-1/faxes', () => HttpResponse.json({ faxes: [
      { id: 'fax-9', from_number: '+15550100001', pages: 1, received_at: '2026-10-07T15:02:00' }] })),
    http.post('/direct/notices/n-1/pair', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...waiting, state: 'paired', matched_by: 'person', detail: 'Paired with the received notice fax. The document is in Received.',
        partner_told: true });
    }),
    http.post('/direct/peers/peer-1/notice-fax', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...partner, notice_fax: true,
        notice_fax_text: 'Each document goes directly, with a one-page notice by fax for their fax intake.',
        detail: 'Each document to County Clinic now goes directly, with a one-page notice by fax.' });
    }),
  );
}

describe('Partners → notice faxes, pieces and repaired calls', () => {
  it('turns on a notice fax for a partner and says what changes', async () => {
    const calls: Recorded[] = [];
    handlers(calls);
    render(<DeliveryRoutes client={client()} canWrite section="partners" />);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Send a notice fax with each document' }));
    expect(await screen.findByText('Each document to County Clinic now goes directly, with a one-page notice by fax.')).toBeTruthy();
    expect(calls).toEqual([{ method: 'POST', path: '/direct/peers/peer-1/notice-fax', body: { on: true } }]);
  });

  it('pairs a held document with the received fax that is its notice, checking the code', async () => {
    const calls: Recorded[] = [];
    handlers(calls);
    render(<DeliveryRoutes client={client()} canWrite section="partners" />);
    expect(await screen.findByText(/waits for its notice fax\. Code 1234 5678 9012 3456 7890\./)).toBeTruthy();
    expect(screen.getByText('Sending in pieces: 12 of 50 pieces confirmed.')).toBeTruthy();
    expect(screen.getByText(/only pages 7 to 10 went directly/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Pair' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(await within(dialog).findByRole('radio', { name: /From \+15550100001, 1 page/ }));
    fireEvent.change(within(dialog).getByLabelText('Code on the page (optional)'), { target: { value: '1234 5678 9012 3456 7890' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Pair' }));
    expect(await screen.findByText('Paired with the received notice fax. The document is in Received.')).toBeTruthy();
    expect(calls).toEqual([{ method: 'POST', path: '/direct/notices/n-1/pair',
      body: { code: '1234 5678 9012 3456 7890', fax_id: 'fax-9' } }]);
  });

  it('files a held document without its notice fax only when that is chosen', async () => {
    const calls: Recorded[] = [];
    handlers(calls);
    render(<DeliveryRoutes client={client()} canWrite section="partners" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Pair' }));
    const dialog = await screen.findByRole('dialog');
    const submit = within(dialog).getByRole('button', { name: 'Pair' }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole('radio', { name: 'File it in Received without a notice fax' }));
    fireEvent.click(submit);
    await waitFor(() => expect(calls).toEqual([{ method: 'POST', path: '/direct/notices/n-1/pair', body: {} }]));
  });

  it('shows on a received notice page which document it announced', async () => {
    server.use(http.get('/direct/notices', ({ request }) => HttpResponse.json(
      new URL(request.url).searchParams.get('fax') === 'fax-9'
        ? { notices: [], notice_text: "Notice for Valley Hospital's 3-page document: the document came by direct delivery." }
        : { notices: [], notice_text: null })));
    render(<ReceivedNotice client={client()} faxId="fax-9" />);
    expect(await screen.findByText("Notice for Valley Hospital's 3-page document: the document came by direct delivery.")).toBeTruthy();
  });
});
