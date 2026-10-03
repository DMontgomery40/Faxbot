// The Inbox shows each received fax's email delivery; there is no separate Intake screen.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Inbound from '../components/Inbound';
import { visibleTools } from '../navigation';
import { formatServerTime, toServerTime } from '../api/time';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const fax = (id: string, from: string, received_at = '2026-09-01T12:00:00') => ({ id, fr: from, to: '+15550100001', status: 'received',
  backend: 'sip', pages: 2, received_at });

const item = (overrides: Record<string, unknown>) => ({
  id: 'item', source: 'fax', inbound_fax_id: null, received_at: '2026-10-03T12:00:00', pages: 2, from_number: '+15550109999',
  to_number: '+15550100001', state: 'received', status: 'Waiting to be delivered.', needs_action: false, attempts: 0,
  next_attempt_at: '2026-10-03T12:00:05', delivered_at: null, connector: null, delivered_to: [], ...overrides,
});

function inbox(retries: string[] = []) {
  server.use(
    http.get('/inbound', () => HttpResponse.json([
      fax('fax-delivered', '+15550101111'), fax('fax-waiting', '+15550102222'), fax('fax-failed', '+15550103333'),
      fax('fax-unrouted', '+15550104444'), fax('fax-new', '+15550105555', toServerTime(new Date())), fax('fax-old', '+15550107777'),
    ])),
    http.get('/admin/inbound/callbacks', () => HttpResponse.json({ callbacks: [] })),
    http.get('/intake/items', ({ request }) => {
      expect(new URL(request.url).searchParams.get('limit')).toBe('500');
      return HttpResponse.json({ items: [
        item({ id: 'i1', inbound_fax_id: 'fax-delivered', state: 'delivered', status: 'Delivered.', next_attempt_at: null,
          delivered_at: '2026-10-03T12:00:09', connector: 'Front desk', delivered_to: ['frontdesk@clinic.example'] }),
        item({ id: 'i2', inbound_fax_id: 'fax-waiting' }),
        item({ id: 'i3', inbound_fax_id: 'fax-failed', state: 'failed', needs_action: true, next_attempt_at: null,
          status: 'The email server refused the recipient address. Faxbot stopped retrying.' }),
        item({ id: 'i4', inbound_fax_id: 'fax-unrouted', needs_action: true, next_attempt_at: null,
          status: 'No email delivery is set up for this number yet.' }),
        item({ id: 'i5', source: 'direct', from_number: '+15550106666', pages: 1, state: 'delivered', status: 'Delivered.',
          next_attempt_at: null, delivered_at: '2026-10-03T13:00:00', delivered_to: ['billing@clinic.example'] }),
      ], counts: { received: 2, sending: 0, delivered: 2, failed: 1 } });
    }),
    http.post('/intake/items/:id/retry', ({ params }) => {
      retries.push(String(params.id));
      return HttpResponse.json(item({ id: params.id, status: 'Waiting to be delivered.' }));
    }),
  );
  return retries;
}

const operator = new Set(['inbound:list', 'inbound:read', 'inbound:document', 'mailboxes:read', 'settings:read', 'settings:write']);
const masked = (number: string) => '*'.repeat(number.length - 4) + number.slice(-4);
const rowFor = async (from: string) => (await screen.findByText(masked(from))).closest('tr') as HTMLElement;

describe('Inbox email delivery', () => {
  it('shows each fax\'s email delivery in plain words', async () => {
    inbox();
    render(<Inbound client={client()} inboundEnabled permissions={operator} onNavigate={() => undefined} />);
    // One column in the fax table and one in the direct delivery table.
    expect(await screen.findAllByRole('columnheader', { name: 'Email delivery' })).toHaveLength(2);
    const delivered = await rowFor('+15550101111');
    expect(within(delivered).getByText('Delivered to frontdesk@clinic.example')).toBeTruthy();
    expect(within(delivered).getByText(formatServerTime('2026-10-03T12:00:09'))).toBeTruthy();
    expect(within(await rowFor('+15550102222')).getByText('Waiting for email delivery')).toBeTruthy();
    const failed = await rowFor('+15550103333');
    expect(within(failed).getByText('Not delivered')).toBeTruthy();
    expect(within(failed).getByText('The email server refused the recipient address. Faxbot stopped retrying.')).toBeTruthy();
    const unrouted = await rowFor('+15550104444');
    expect(within(unrouted).getByText('No email delivery set up for this number')).toBeTruthy();
    // Just received and not picked up yet: simply waiting.
    expect(within(await rowFor('+15550105555')).getByText('Waiting for email delivery')).toBeTruthy();
    // An older fax with no delivery record (no document, or older than the list read): nothing is claimed.
    const old = await rowFor('+15550107777');
    expect(within(old).queryByText(/Waiting|Delivered|Not delivered/)).toBeNull();
    expect(within(old).getByText('-')).toBeTruthy();
    // Direct deliveries have no fax record; they are listed with their delivery too.
    expect(screen.getByText('Received by direct delivery')).toBeTruthy();
    expect(screen.getByText('Delivered to billing@clinic.example')).toBeTruthy();
  });

  it('retries a delivery that did not go through', async () => {
    const retries = inbox();
    render(<Inbound client={client()} inboundEnabled permissions={operator} />);
    const failed = await rowFor('+15550103333');
    fireEvent.click(within(failed).getByRole('button', { name: `Retry delivery of the fax from ${masked('+15550103333')}` }));
    expect(await screen.findByText('Faxbot will deliver it shortly.')).toBeTruthy();
    expect(retries).toEqual(['i3']);
    expect(within(await rowFor('+15550101111')).queryByRole('button', { name: /Retry delivery/ })).toBeNull();
  });

  it('sends a fax that arrived before email delivery covered its number, once it does', async () => {
    const retries = inbox();
    render(<Inbound client={client()} inboundEnabled permissions={operator} />);
    const unrouted = await rowFor('+15550104444');
    fireEvent.click(within(unrouted).getByRole('button', { name: `Retry delivery of the fax from ${masked('+15550104444')}` }));
    expect(await screen.findByText('Faxbot will deliver it shortly.')).toBeTruthy();
    expect(retries).toEqual(['i4']);
  });

  it('offers no retry to people who cannot change settings', async () => {
    inbox();
    render(<Inbound client={client()} inboundEnabled permissions={new Set(['inbound:list', 'mailboxes:read'])} />);
    expect(within(await rowFor('+15550103333')).getByText('Not delivered')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Retry delivery/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Email delivery settings' })).toBeNull();
  });

  it('shows the Inbox without a delivery column, or an error, when the account cannot read deliveries', async () => {
    let asked = 0;
    inbox();
    server.use(http.get('/intake/items', () => { asked += 1; return HttpResponse.json({ detail: 'Forbidden' }, { status: 403 }); }));
    render(<Inbound client={client()} inboundEnabled permissions={new Set(['inbound:list', 'inbound:read'])} />);
    await rowFor('+15550101111');
    expect(screen.queryByRole('columnheader', { name: 'Email delivery' })).toBeNull();
    expect(screen.queryByText(/Waiting for email delivery|Forbidden|could not/i)).toBeNull();
    expect(asked).toBe(0);
  });

  it('hides the column quietly if the delivery list is refused anyway', async () => {
    inbox();
    server.use(http.get('/intake/items', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    render(<Inbound client={client()} inboundEnabled permissions={operator} />);
    await rowFor('+15550101111');
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole('columnheader', { name: 'Email delivery' })).toBeNull();
    expect(screen.queryByText(/Forbidden|permission/i)).toBeNull();
  });

  it('links to the email delivery settings', async () => {
    inbox();
    const navigate = vi.fn();
    render(<Inbound client={client()} inboundEnabled permissions={operator} onNavigate={navigate} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Email delivery settings' }));
    expect(navigate).toHaveBeenCalledWith('email');
  });

  it('has no Intake tool any more', () => {
    const tools = visibleTools(new Set(['mailboxes:read', 'settings:read', 'diagnostics:read']), false).map((tool) => tool.label);
    expect(tools).not.toContain('Intake');
    expect(tools).toContain('Delivery routes');
  });
});

describe('Inbox on phones', () => {
  it('shows the delivery line on each fax card', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({ ...original(query), matches: query.includes('max-width') })) as typeof window.matchMedia;
    try {
      inbox();
      render(<Inbound client={client()} inboundEnabled permissions={operator} />);
      await waitFor(() => expect(screen.getAllByText('Email delivery').length).toBeGreaterThan(0));
      expect(await screen.findByText('Delivered to frontdesk@clinic.example')).toBeTruthy();
      expect(screen.queryByRole('columnheader', { name: 'Email delivery' })).toBeNull();
    } finally {
      window.matchMedia = original;
    }
  });
});
