import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Dashboard from '../components/Dashboard';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const provider = (provider_id: string, label: string, amount: string) => ({
  provider_id, label, attempts: 4, successes: 4, failures: 0, uncertain: 0, billed_minutes: 6, billed_pages: 12,
  estimated_cost: [{ currency: 'USD', amount }], reported_cost: [], settled_cost: [], attempts_without_reported_cost: 4,
});

const peer = (id: string, state: string) => ({
  id, organization: id, fax_number: '+15550100001', endpoint: 'https://partner.example', state, status: '', code_sent: false,
  code_expires_at: null, verified_at: null, expires_at: null, version: 1,
});

describe('Dashboard delivery cards', () => {
  it('shows spending by provider, email delivery counts and verified partners, each opening its screen', async () => {
    server.use(
      http.get('/routing/costs', () => HttpResponse.json({ since: '2026-09-03T00:00:00', providers: [
        provider('sip', 'SIP trunk (Asterisk)', '1.25'), provider('phaxio', 'Phaxio', '3.50'),
      ] })),
      http.get('/intake/items', () => HttpResponse.json({ items: [], counts: { received: 2, sending: 1, delivered: 40, failed: 1 } })),
      http.get('/direct/peers', () => HttpResponse.json({ peers: [peer('a', 'verified'), peer('b', 'verified'), peer('c', 'pending'), peer('d', 'revoked')] })),
    );
    const navigate = vi.fn();
    render(<Dashboard client={client()} onNavigate={navigate} />);

    const spending = await screen.findByRole('button', { name: 'Spending, last 30 days' });
    expect(spending.textContent).toContain('SIP trunk (Asterisk)$1.25');
    expect(spending.textContent).toContain('Phaxio$3.50');
    expect(spending.textContent).toContain('Total$4.75');

    const delivery = await screen.findByRole('button', { name: 'Email delivery' });
    expect(delivery.textContent).toContain('Waiting3');
    expect(delivery.textContent).toContain('Delivered40');
    expect(delivery.textContent).toContain('Not delivered1');

    const partners = await screen.findByRole('button', { name: 'Direct partners' });
    expect(partners.textContent).toContain('Verified2');
    expect(partners.textContent).toContain('Waiting for verification1');

    fireEvent.click(spending);
    fireEvent.click(delivery);
    fireEvent.click(partners);
    expect(navigate.mock.calls.map(([destination]) => destination)).toEqual(['routes', 'inbox', 'routes']);
  });

  it('says a card is not available to this account when permission is missing', async () => {
    const denied = () => HttpResponse.json({ detail: 'This operation is not permitted.' }, { status: 403 });
    server.use(http.get('/routing/costs', denied), http.get('/intake/items', denied), http.get('/direct/peers', denied));
    const navigate = vi.fn();
    render(<Dashboard client={client()} onNavigate={navigate} />);
    expect(await screen.findAllByText('Not available to this account.')).toHaveLength(3);
    expect(screen.queryByRole('button', { name: 'Spending, last 30 days' })).toBeNull();
    fireEvent.click(screen.getByText('Spending, last 30 days'));
    expect(navigate).not.toHaveBeenCalled();
  });

  it('names a received trunk call from the last day that left no fax', async () => {
    const recent = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    const old = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();
    const call = (id: string, startedAt: string, jobId: string | null, summary: string) => ({
      id, direction: 'inbound', job_id: jobId, attempt_id: null, trunk_preset: 'telnyx', did: '+15555550100',
      caller: '+13035550100', called: '+15555550100', started_at: startedAt, answered_at: startedAt, ended_at: startedAt,
      disposition: 'answered', connected_seconds: 14, t38: 'yes', pages: 0, fax_status: 'FAILED',
      remote_station_id: null, error_cause: null, fax_preference: false, verdict: null, summary });
    server.use(http.get('/admin/sip/calls', () => HttpResponse.json({ items: [
      call('stored', recent, 'f'.repeat(32), 'Received: 2 pages.'),
      call('missed', recent, null, 'A fax call from +13035550100 came in, but no pages arrived.'),
      call('older', old, null, 'A fax call from +13035550199 came in, but no fax data arrived from the carrier.'),
    ], next_cursor: null })));
    render(<Dashboard client={client()} />);
    expect((await screen.findByTestId('missed-inbound-call')).textContent)
      .toBe('A fax call from +13035550100 came in, but no pages arrived.');
  });

  it('says nothing about trunk calls when there was no missed call or no access to call history', async () => {
    server.use(http.get('/admin/sip/calls', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    render(<Dashboard client={client()} />);
    expect(await screen.findByText('Inbound Fax')).toBeTruthy();
    expect(screen.queryByTestId('missed-inbound-call')).toBeNull();
  });

  it('says plainly when nothing was sent', async () => {
    render(<Dashboard client={client()} />);
    expect(await screen.findByText('No faxes sent or received in the last 30 days.')).toBeTruthy();
  });

  it('offers one clear action when no fax provider is set up', async () => {
    server.use(http.get('/admin/health-status', () => HttpResponse.json({ timestamp: new Date().toISOString(),
      backend: '', backend_healthy: false, backend_message: 'No fax provider set up yet.',
      jobs: { queued: 0, in_progress: 0, recent_failures: 0 }, inbound_enabled: false, api_keys_configured: true, require_auth: true })));
    const navigate = vi.fn();
    const { unmount } = render(<Dashboard client={client()} onNavigate={navigate} canSetUp />);
    fireEvent.click(await screen.findByRole('button', { name: 'Set up a fax provider' }));
    expect(navigate).toHaveBeenCalledWith('setup');
    expect(navigate).not.toHaveBeenCalledWith('diagnostics');
    unmount();
    render(<Dashboard client={client()} onNavigate={navigate} />);
    expect(await screen.findByText('No fax provider set up yet.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Set up a fax provider' })).toBeNull();
  });

  it('says in one sentence when Faxbot cannot sign in to its fax engine', async () => {
    const sentence = "Faxbot can't sign in to its fax engine. Check that the Asterisk manager password matches.";
    server.use(http.get('/admin/health-status', () => HttpResponse.json({ timestamp: new Date().toISOString(),
      backend: 'sip', backend_healthy: false, backend_message: sentence,
      jobs: { queued: 0, in_progress: 0, recent_failures: 0 }, inbound_enabled: true, api_keys_configured: true, require_auth: true })));
    render(<Dashboard client={client()} />);
    expect((await screen.findByTestId('engine-message')).textContent).toBe(sentence);
    expect(screen.getByText('Needs attention')).toBeTruthy();
  });
});

describe('Dashboard providers', () => {
  it('names what sends and what receives faxes, the same way as System Status', async () => {
    server.use(http.get('/admin/config', () => HttpResponse.json({ backend: 'humblefax',
      hybrid: { outbound: 'humblefax', inbound: 'sip', outbound_explicit: false, inbound_explicit: true },
      inbound: { enabled: true }, storage: { backend: 'local' } })));
    render(<Dashboard client={client()} />);
    expect((await screen.findByTestId('config-sending')).textContent).toBe('HumbleFax');
    expect(screen.getByTestId('config-receiving').textContent).toBe('SIP trunk (Asterisk)');
    expect(screen.queryByText('Default provider')).toBeNull();
  });
});

describe('Dashboard authentication', () => {
  it('states authentication is required and never that it is optional', async () => {
    render(<Dashboard client={client()} />);
    expect(await screen.findByText('Authentication required')).toBeTruthy();
    expect(screen.queryByText('Auth Optional')).toBeNull();
    expect(screen.queryByText('Require API Key')).toBeNull();
  });
});
