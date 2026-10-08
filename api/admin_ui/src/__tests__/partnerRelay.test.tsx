import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { RelayAgreement } from '../api/deliveryTypes';
import DeliveryRoutes from '../components/DeliveryRoutes';
import RelayCosts from '../components/delivery/RelayCosts';
import RelayRecommendations from '../components/delivery/RelayRecommendations';
import { server } from '../test/server';

type Recorded = { method: string; path: string; body: unknown };

function recorder() {
  const calls: Recorded[] = [];
  const record = async (request: Request) => {
    const text = await request.text();
    calls.push({ method: request.method, path: new URL(request.url).pathname, body: text ? JSON.parse(text) : null });
  };
  return { calls, record };
}

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const partner = {
  id: 'peer-1', organization: 'Sydney office', fax_number: '+61255501234', endpoint: 'https://sydney.example',
  state: 'verified', status: 'Verified; faxes to this number are delivered directly.', code_sent: false,
  code_expires_at: null, verified_at: '2026-10-07T01:00:00', expires_at: null, version: 3,
};

const PRIVACY = 'Sydney office will see what these faxes contain. If they are another organization, check that '
  + 'your agreement with them covers this; Faxbot does not make relaying exempt from any privacy rules.';

const offered: RelayAgreement = {
  id: 'a'.repeat(32), peer_id: 'peer-1', partner: 'Sydney office', role: 'sender', state: 'offered',
  summary: 'Send our faxes to numbers in Australia through Sydney office, up to 500 pages a month.',
  status: 'Sydney office offers to send your faxes to numbers in Australia as local calls; accept to use it.',
  notes: [PRIVACY, 'Sydney office receives each document over the internet and places its own fax call; no call is passed through.'],
  countries: ['AU'], regions: [], monthly_pages: 500, monthly_spend: null, hours: null, together: true,
  send_together: false, same_organization: false, reply_number: null, marketing: false,
  price: { priced_at: '2026-10-07T01:00:00Z', valid_until: '2026-11-07T01:00:00Z',
    routes: [{ country: 'AU', kind: 'local', text: 'Numbers in Australia: 0.01 AUD a minute' }] },
  version: 1,
};

function partnersHandlers(record: (request: Request) => Promise<void>, agreements: RelayAgreement[] = []) {
  server.use(
    http.get('/direct/peers', () => HttpResponse.json({ peers: [partner] })),
    http.get('/direct/relay/agreements', () => HttpResponse.json({ agreements })),
    http.post('/direct/relay/agreements', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...offered, role: 'relay', state: 'offered',
        summary: 'Let Sydney office send faxes to numbers in Australia through us, up to 500 pages a month.',
        status: 'Offered; waiting for Sydney office to accept.', detail: 'Sydney office has your offer; they accept it from their Faxbot.' },
      { status: 201 });
    }),
    http.post('/direct/relay/agreements/:id/accept', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...offered, state: 'active', detail: 'In force: your faxes can go through Sydney office.' });
    }),
    http.post('/direct/relay/agreements/:id/withdraw', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...offered, state: 'withdrawn', detail: 'Ended. Faxes already accepted for relaying still go.' });
    }),
  );
}

describe('Partners → Relay', () => {
  it('offers to relay a partner\'s faxes, showing what the relay will see before anything is signed', async () => {
    const { calls, record } = recorder();
    partnersHandlers(record);
    render(<DeliveryRoutes client={client()} canWrite section="partners" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Relay' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('No relay agreement with Sydney office yet.')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: "Offer to send Sydney office's faxes" }));
    fireEvent.change(within(dialog).getByLabelText('Countries, such as AU'), { target: { value: 'au' } });
    fireEvent.change(within(dialog).getByLabelText('Most pages a month'), { target: { value: '500' } });
    expect(within(dialog).getByText(/You will see what Sydney office's faxes contain/)).toBeTruthy();
    expect(within(dialog).getByTestId('relay-resale').textContent).toContain("Telnyx's terms make you responsible");
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Sydney office is part of our own organization' }));
    expect(within(dialog).queryByTestId('relay-resale')).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Make the offer' }));
    expect(await within(dialog).findByText('Sydney office has your offer; they accept it from their Faxbot.')).toBeTruthy();
    expect(calls.find((call) => call.path === '/direct/relay/agreements')?.body).toEqual({
      partner: 'peer-1', countries: ['AU'], monthly_pages: 500, monthly_spend: null, hours: null, together: false,
      same_organization: true,
    });
  });

  it('accepts a partner\'s offer with the number replies should reach, and can end it', async () => {
    const { calls, record } = recorder();
    partnersHandlers(record, [offered]);
    render(<DeliveryRoutes client={client()} canWrite section="partners" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Relay' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(offered.summary)).toBeTruthy();
    expect(within(dialog).getByText(PRIVACY)).toBeTruthy();
    expect(within(dialog).getByText('Numbers in Australia: 0.01 AUD a minute')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Accept' }));
    fireEvent.change(within(dialog).getByLabelText('Number for replies'), { target: { value: '+441134960999' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Accept the offer' }));
    expect(await within(dialog).findByText('In force: your faxes can go through Sydney office.')).toBeTruthy();
    expect(calls.find((call) => call.path.endsWith('/accept'))?.body).toEqual({
      reply_number: '+441134960999', together: true, same_organization: false, marketing: null,
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'End agreement' }));
    expect(await within(dialog).findByText('Ended. Faxes already accepted for relaying still go.')).toBeTruthy();
    expect(calls.some((call) => call.path.endsWith('/withdraw'))).toBe(true);
  });

  it('lets people who can only read settings see agreements but change nothing', async () => {
    partnersHandlers(async () => undefined, [offered]);
    render(<DeliveryRoutes client={client()} canWrite={false} section="partners" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Relay' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(offered.summary)).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: 'Accept' })).toBeNull();
    expect(within(dialog).queryByRole('button', { name: 'End agreement' })).toBeNull();
  });
});

describe('Costs: partner relays', () => {
  it('shows each side\'s money and the relayed faxes, and stays hidden when nothing was relayed', async () => {
    const { unmount } = render(<RelayCosts client={client()} />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId('relay-costs')).toBeNull();
    unmount();
    server.use(
      http.get('/direct/relay/costs', () => HttpResponse.json({ days: 30, agreements: [
        { agreement_id: 'a1', role: 'sender', partner: 'Sydney office', faxes: 12, pages: 14,
          amounts: [{ amount: '0.84', currency: 'USD' }], own_route: [{ amount: '9.60', currency: 'USD' }],
          sentence: 'Sent through Sydney office: 12 faxes, $0.84, against about $9.60 calling from the UK.' },
      ] })),
      http.get('/direct/relay/faxes', () => HttpResponse.json({ faxes: [
        { fax_id: 'f1', role: 'sender', partner: 'Sydney office', fax_number: '+61755501234', pages: 2, seconds: 41,
          state: 'uncertain', shared: false, status: 'Sydney office cannot confirm whether the fax arrived; it is checking.',
          created_at: '2026-10-07T01:00:00' },
      ] })),
    );
    render(<RelayCosts client={client()} />);
    expect(await screen.findByText('Sent through Sydney office: 12 faxes, $0.84, against about $9.60 calling from the UK.')).toBeTruthy();
    expect(screen.getByText('Sydney office cannot confirm whether the fax arrived; it is checking.')).toBeTruthy();
  });
});

describe('Recommendations: partner relays', () => {
  it('says how much a partner would have saved and what to do', async () => {
    server.use(http.get('/direct/relay/recommendations', () => HttpResponse.json({ days: 30, recommendations: [
      { peer_id: 'peer-1', partner: 'Sydney office', country: 'AU', agreement_id: 'a1', agreement_state: 'offered',
        faxes: 12, saving: { amount_micros: 8760000, currency: 'USD' },
        sentence: 'Faxes to numbers in Australia would have cost about $8.76 less through Sydney office.',
        action: "Accept Sydney office's offer under Partners to use it." },
    ] })));
    let counted: number | null = null;
    render(<RelayRecommendations client={client()} onCount={(count) => { counted = count; }} />);
    expect(await screen.findByText('Faxes to numbers in Australia would have cost about $8.76 less through Sydney office.')).toBeTruthy();
    expect(screen.getByText("Accept Sydney office's offer under Partners to use it.")).toBeTruthy();
    expect(counted).toBe(1);
  });
});
