import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import DeliveryRoutes from '../components/DeliveryRoutes';
import Intake from '../components/Intake';
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

const destination = {
  number: '+12025550123', display_name: 'County clinic', notes: null, preferred_route: null, accepts_references: false,
  version: 3, estimated_cost_30_days: [{ currency: 'USD', amount: '1.25' }],
  routes: [{ route: 'sip', label: 'Your SIP trunk (Asterisk)', attempts: 10, successes: 9, failures: 1, uncertain: 0,
    success_percent: 90, estimated_cost_30_days: [{ currency: 'USD', amount: '1.25' }], reported_cost_30_days: [],
    last_attempt_at: '2026-10-03T12:00:00' }],
};

function routingHandlers(record: (request: Request) => Promise<void>) {
  server.use(
    http.get('/routing/destinations', () => HttpResponse.json({ window_days: 30, destinations: [destination] })),
    http.get('/routing/destinations/:number', () => HttpResponse.json({
      ...destination, direct_partner: null,
      recommended_routes: [
        { route: 'sip', label: 'Your SIP trunk (Asterisk)', reason: 'cheapest', explanation: 'The cheapest route that works reliably for this number.', estimated_cost_one_page: { currency: 'USD', amount: '0.005' } },
        { route: 'phaxio', label: 'Phaxio', reason: 'alternative', explanation: 'Used if the routes above it are unavailable.', estimated_cost_one_page: { currency: 'USD', amount: '0.07' } },
      ],
      available_routes: [{ route: 'phaxio', label: 'Phaxio' }, { route: 'sip', label: 'Your SIP trunk (Asterisk)' }],
    })),
    http.patch('/routing/destinations/:number', async ({ request }) => { await record(request); return HttpResponse.json(destination); }),
    http.get('/routing/costs', () => HttpResponse.json({ since: '2026-09-03T00:00:00', providers: [
      { provider_id: 'sip', label: 'Your SIP trunk (Asterisk)', attempts: 10, successes: 9, failures: 1, uncertain: 0,
        billed_minutes: 33, billed_pages: 120, estimated_cost: [{ currency: 'USD', amount: '1.25' }], reported_cost: [],
        settled_cost: [], attempts_without_reported_cost: 10 },
    ] })),
    http.get('/routing/rate-cards', () => HttpResponse.json({ cards: [
      { id: 'card-1', provider_id: 'sip', label: 'Telnyx SIP trunk', direction: 'outbound', currency: 'USD', per_minute: '0.005',
        per_page: '0.00', per_call: '0.00', billing_increment_seconds: 60, minimum_seconds: 0,
        source_url: 'https://telnyx.com/pricing/elastic-sip', captured_on: '2026-10-03' },
    ] })),
    http.put('/routing/rate-cards', async ({ request }) => { await record(request); return HttpResponse.json({ cards: [] }); }),
    http.get('/direct/peers', () => HttpResponse.json({ peers: [
      { id: 'peer-1', organization: 'Valley Hospital', fax_number: '+15550100001', endpoint: 'https://valley.example',
        state: 'pending', status: 'Send a challenge fax so the partner can confirm this number.', code_sent: false,
        code_expires_at: null, verified_at: null, expires_at: null, version: 1 },
    ] })),
    http.post('/direct/peers/:id/confirm', async ({ request }) => {
      await record(request);
      return HttpResponse.json({ detail: 'The code did not match.' }, { status: 409 });
    }),
  );
}

describe('delivery routes', () => {
  it('shows money as money and saves a preferred route for a number', async () => {
    const { calls, record } = recorder();
    routingHandlers(record);
    render(<DeliveryRoutes client={client()} canWrite />);
    expect(await screen.findByText('Spending')).toBeTruthy();
    expect(screen.getAllByText('$1.25').length).toBeGreaterThan(0);
    expect(screen.getByText(/10 faxes, 9 delivered, 33 minutes, 120 pages/)).toBeTruthy();
    expect(screen.getByText('$0.005 per minute')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Details for +12025550123' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('The cheapest route that works reliably for this number.')).toBeTruthy();
    expect(within(dialog).getByText(/about \$0\.005 for one page/)).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Preferred route'), { target: { value: 'phaxio' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText('Spending');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({
      display_name: 'County clinic', notes: null, preferred_route: 'phaxio', accepts_references: false, version: 3,
    });
  });

  it('adds a rate card by saving the complete set', async () => {
    const { calls, record } = recorder();
    routingHandlers(record);
    render(<DeliveryRoutes client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add rate card' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add rate card' });
    fireEvent.change(within(dialog).getByLabelText('Provider'), { target: { value: 'signalwire' } });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'SignalWire fax' } });
    fireEvent.change(within(dialog).getByLabelText('Per minute (USD)'), { target: { value: '0.0095' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await new Promise((resolve) => setTimeout(resolve, 50));
    const body = calls.find((call) => call.method === 'PUT')?.body as { cards: Array<Record<string, unknown>> };
    expect(body.cards.map((card) => card.provider_id)).toEqual(['sip', 'signalwire']);
    expect(body.cards[1]).toMatchObject({ label: 'SignalWire fax', per_minute: '0.0095', billing_increment_seconds: 60 });
    expect(body.cards.every((card) => !('id' in card))).toBe(true);
  });

  it('shows the partner answer when a confirmation code does not match', async () => {
    const { calls, record } = recorder();
    routingHandlers(record);
    render(<DeliveryRoutes client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm a code' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Code'), { target: { value: '1234 5678' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }));
    expect(await within(dialog).findByText('The code did not match.')).toBeTruthy();
    expect(calls.find((call) => call.path === '/direct/peers/peer-1/confirm')?.body).toEqual({ code: '1234 5678' });
  });

  it('hides changes from people who can only read settings', async () => {
    routingHandlers(async () => undefined);
    render(<DeliveryRoutes client={client()} canWrite={false} />);
    expect(await screen.findByText('Telnyx SIP trunk')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add rate card' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add partner' })).toBeNull();
  });
});

describe('intake', () => {
  function intakeHandlers(record: (request: Request) => Promise<void>) {
    server.use(
      http.get('/intake/items', () => HttpResponse.json({
        items: [
          { id: 'item-1', source: 'fax', received_at: '2026-10-03T12:00:00', pages: 3, from_number: '+15550109999',
            to_number: '+15550100001', state: 'received', status: 'This fax arrived before email delivery was set up; send it when you are ready.',
            needs_action: true, attempts: 0, next_attempt_at: null, delivered_at: null, connector: null },
          { id: 'item-2', source: 'direct', received_at: '2026-10-03T13:00:00', pages: 1, from_number: '+15550100002',
            to_number: '+15550100001', state: 'delivered', status: 'Delivered.', needs_action: false, attempts: 1,
            next_attempt_at: null, delivered_at: '2026-10-03T13:00:05', connector: 'Front desk' },
        ],
        counts: { received: 1, sending: 0, delivered: 1, failed: 0 },
      })),
      http.post('/intake/items/:id/retry', async ({ request }) => { await record(request); return HttpResponse.json({}); }),
      http.get('/intake/connectors', () => HttpResponse.json({ connectors: [
        { id: 'c-1', kind: 'email', name: 'Front desk', enabled: true, match_number: null, host: 'smtp.example.org',
          port: 587, security: 'starttls', username: 'fax', has_password: true, from_address: 'fax@example.org',
          recipients: ['frontdesk@example.org'], subject_template: 'Fax from {from_number}', managed: false, version: 2 },
      ] })),
      http.post('/intake/connectors', async ({ request }) => { await record(request); return HttpResponse.json({}, { status: 201 }); }),
      http.post('/intake/connectors/:id/test', () => HttpResponse.json({ ok: false, detail: 'Faxbot could not reach the email server.' })),
    );
  }

  it('lists received documents with plain status and sends a waiting fax now', async () => {
    const { calls, record } = recorder();
    intakeHandlers(record);
    render(<Intake client={client()} canWrite />);
    expect(await screen.findByText('Fax from +15550109999, 3 pages')).toBeTruthy();
    expect(screen.getByText('Direct delivery from +15550100002, 1 page')).toBeTruthy();
    expect(screen.getByText('1 waiting')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Send Fax from +15550109999, 3 pages now' }));
    expect(await screen.findByText('Faxbot will deliver it shortly.')).toBeTruthy();
    expect(calls.map((call) => call.path)).toContain('/intake/items/item-1/retry');
  });

  it('adds email delivery and reports a failed test in a plain sentence', async () => {
    const { calls, record } = recorder();
    intakeHandlers(record);
    render(<Intake client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Send test email' }));
    expect(await screen.findByText('Faxbot could not reach the email server.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add email delivery' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add email delivery' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Billing' } });
    fireEvent.change(within(dialog).getByLabelText('Recipients'), { target: { value: 'a@example.org, b@example.org' } });
    fireEvent.change(within(dialog).getByLabelText('Email server'), { target: { value: 'smtp.example.org' } });
    fireEvent.change(within(dialog).getByLabelText('Sent from'), { target: { value: 'fax@example.org' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Email delivery saved.')).toBeTruthy();
    expect(calls.find((call) => call.path === '/intake/connectors')?.body).toMatchObject({
      name: 'Billing', recipients: ['a@example.org', 'b@example.org'], host: 'smtp.example.org', port: 587,
      security: 'starttls', from_address: 'fax@example.org', match_number: null,
    });
  });
});
