// The Inbox shows each received fax's email delivery; there is no separate Intake screen.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Received from '../components/Received';
import ReceivingAddresses from '../components/delivery/ReceivingAddresses';
import { emailDeliveryApplies, inboundFaxStatus, inboxDeliveryStatus, providerName } from '../components/delivery/InboxDelivery';
import { visibleNavigation } from '../navigation';
import { formatServerTime, parseServerTime, toServerTime } from '../api/time';
import type { EmailConnector, IntakeItem } from '../api/deliveryTypes';
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

const emailConnector = (match_number: string | null, enabled = true): EmailConnector => ({
  id: `connector-${match_number ?? 'all'}`, kind: 'email', name: 'Front desk', enabled, match_number, host: 'smtp.clinic.example',
  port: 587, security: 'starttls', username: '', has_password: false, from_address: 'fax@clinic.example',
  recipients: ['frontdesk@clinic.example'], subject_template: 'Fax from {from_number}', managed: false, version: 1,
});
const masked = (number: string) => '*'.repeat(number.length - 4) + number.slice(-4);
const rowFor = async (from: string) => (await screen.findByText(masked(from))).closest('tr') as HTMLElement;

describe('Inbox email delivery', () => {
  it('shows each fax\'s email delivery in plain words', async () => {
    inbox();
    render(<Received client={client()} inboundEnabled permissions={operator} onNavigate={() => undefined} />);
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
    render(<Received client={client()} inboundEnabled permissions={operator} />);
    const failed = await rowFor('+15550103333');
    fireEvent.click(within(failed).getByRole('button', { name: `Retry delivery of the fax from ${masked('+15550103333')}` }));
    expect(await screen.findByText('Faxbot will deliver it shortly.')).toBeTruthy();
    expect(retries).toEqual(['i3']);
    expect(within(await rowFor('+15550101111')).queryByRole('button', { name: /Retry delivery/ })).toBeNull();
  });

  it('sends a fax that arrived before email delivery covered its number, once it does', async () => {
    const retries = inbox();
    server.use(http.get('/intake/connectors', () => HttpResponse.json({ connectors: [emailConnector(null)] })));
    render(<Received client={client()} inboundEnabled permissions={operator} />);
    const unrouted = await rowFor('+15550104444');
    fireEvent.click(within(unrouted).getByRole('button', { name: `Retry delivery of the fax from ${masked('+15550104444')}` }));
    expect(await screen.findByText('Faxbot will deliver it shortly.')).toBeTruthy();
    expect(retries).toEqual(['i4']);
  });

  it('offers no retry to people who cannot change settings', async () => {
    inbox();
    render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list', 'mailboxes:read'])} />);
    expect(within(await rowFor('+15550103333')).getByText('Not delivered')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Retry delivery/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Email delivery settings' })).toBeNull();
  });

  it('shows the Inbox without a delivery column, or an error, when the account cannot read deliveries', async () => {
    let asked = 0;
    inbox();
    server.use(http.get('/intake/items', () => { asked += 1; return HttpResponse.json({ detail: 'Forbidden' }, { status: 403 }); }));
    render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list', 'inbound:read'])} />);
    await rowFor('+15550101111');
    expect(screen.queryByRole('columnheader', { name: 'Email delivery' })).toBeNull();
    expect(screen.queryByText(/Waiting for email delivery|Forbidden|could not/i)).toBeNull();
    expect(asked).toBe(0);
  });

  it('hides the column quietly if the delivery list is refused anyway', async () => {
    inbox();
    server.use(http.get('/intake/items', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    render(<Received client={client()} inboundEnabled permissions={operator} />);
    await rowFor('+15550101111');
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole('columnheader', { name: 'Email delivery' })).toBeNull();
    expect(screen.queryByText(/Forbidden|permission/i)).toBeNull();
  });

  it('links to the email delivery settings', async () => {
    inbox();
    const navigate = vi.fn();
    render(<Received client={client()} inboundEnabled permissions={operator} onNavigate={navigate} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Email delivery settings' }));
    expect(navigate).toHaveBeenCalledWith('email');
  });

  it('has no Intake page any more', () => {
    const pages = visibleNavigation(new Set(['mailboxes:read', 'settings:read', 'diagnostics:read']),
      { send: false, jobs: false, inbox: true }, { pluginsEnabled: false }).flatMap((area) => area.pages.map((page) => page.label));
    expect(pages).not.toContain('Intake');
    expect(pages).toEqual(expect.arrayContaining(['Email delivery', 'Recipients', 'Spending']));
  });
});

describe('Inbox on phones', () => {
  it('shows the delivery line on each fax card', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({ ...original(query), matches: query.includes('max-width') })) as typeof window.matchMedia;
    try {
      inbox();
      render(<Received client={client()} inboundEnabled permissions={operator} />);
      await waitFor(() => expect(screen.getAllByText('Email delivery').length).toBeGreaterThan(0));
      expect(await screen.findByText('Delivered to frontdesk@clinic.example')).toBeTruthy();
      expect(screen.queryByRole('columnheader', { name: 'Email delivery' })).toBeNull();
    } finally {
      window.matchMedia = original;
    }
  });
});

describe('Received fax status', () => {
  it('maps every acquisition state to one chip and at most one sentence', () => {
    const retryAt = '2026-10-03T14:05:00';
    const local = parseServerTime(retryAt)!.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    expect(inboundFaxStatus({ status: 'waiting', status_text: 'Waiting for the document from Phaxio.' })).toEqual({
      label: 'Waiting for the document', detail: 'Waiting for the document from Phaxio.', tone: 'info', hasDocument: false, canFetchAgain: true });
    expect(inboundFaxStatus({ status: 'waiting', status_text: 'The document could not be fetched; Faxbot will try again at 08:05.',
      retry_at: retryAt }).detail).toBe(`The document could not be fetched; Faxbot will try again at ${local}.`);
    expect(inboundFaxStatus({ status: 'failed', status_text: 'Faxbot stopped trying to fetch this document; select Fetch again.' }))
      .toMatchObject({ label: 'Not received', tone: 'error', hasDocument: false, canFetchAgain: true });
    expect(inboundFaxStatus({ status: 'received', status_text: 'Received.' })).toEqual({
      label: 'Received', detail: null, tone: 'success', hasDocument: true, canFetchAgain: false });
    expect(inboundFaxStatus({ status: 'received', status_text: 'This fax arrived earlier and is kept as received.' }).detail)
      .toBe('This fax arrived earlier and is kept as received.');
    expect(inboundFaxStatus({ status: 'received', status_text: 'A test fax created in Faxbot.', is_test: true }))
      .toMatchObject({ label: 'Test fax', hasDocument: true });
    // Faxes recorded before acquisition records keep their provider's status word.
    expect(inboundFaxStatus({ status: 'SUCCESS' })).toMatchObject({ label: 'Received', hasDocument: true });
    // An older fax that never got its document has nothing to fetch from.
    expect(inboundFaxStatus({ status: 'failed', can_fetch_again: false,
      status_text: 'Faxbot never received the document for this fax; ask the sender to send it again.' }))
      .toMatchObject({ label: 'Not received', canFetchAgain: false });
    expect([providerName('sip'), providerName('phaxio'), providerName('sinch'), providerName(undefined)])
      .toEqual(['Carrier trunk', 'Phaxio', 'Sinch', '-']);
  });

  it('offers no delivery retry while no email delivery covers the number', () => {
    const unrouted = item({ id: 'u', needs_action: true, next_attempt_at: null,
      status: 'No email delivery is set up for this number yet.' }) as IntakeItem;
    expect(emailDeliveryApplies([], '+15550100001')).toBe(false);
    expect(emailDeliveryApplies([emailConnector('+15550100002')], '+15550100001')).toBe(false);
    expect(emailDeliveryApplies([emailConnector(null, false)], '+15550100001')).toBe(false);
    expect(emailDeliveryApplies([emailConnector('+15550100001')], '+15550100001')).toBe(true);
    expect(inboxDeliveryStatus(unrouted, false, false)).toMatchObject({ label: 'No email delivery set up for this number', retry: false });
    expect(inboxDeliveryStatus(unrouted, false, true)?.retry).toBe(true);
  });

  it('shows a waiting fax without a download or delivery retry, and fetches it again on request', async () => {
    const fetches: string[] = [];
    server.use(
      http.get('/inbound', () => HttpResponse.json([
        { ...fax('fax-waiting', '+15550108888'), status: 'waiting', pages: null, can_fetch_again: true,
          status_text: 'Waiting for the document from the SIP trunk.' },
        { ...fax('fax-test', '+15550109999'), is_test: true, status_text: 'A test fax created in Faxbot.' },
      ])),
      http.get('/admin/inbound/callbacks', () => HttpResponse.json({ callbacks: [] })),
      http.get('/intake/items', () => HttpResponse.json({ items: [], counts: { received: 0, sending: 0, delivered: 0, failed: 0 } })),
      http.post('/inbound/:id/fetch', ({ params }) => {
        fetches.push(String(params.id));
        return HttpResponse.json({ ...fax(String(params.id), '+15550108888'), status: 'waiting' });
      }),
    );
    render(<Received client={client()} inboundEnabled permissions={new Set([...operator, 'providers:write'])} />);
    const waiting = await rowFor('+15550108888');
    expect(within(waiting).getByText('Waiting for the document from the SIP trunk.')).toBeTruthy();
    expect(within(waiting).getByText('Carrier trunk')).toBeTruthy();
    // No record IDs or raw provider words in the table.
    expect(screen.queryByRole('columnheader', { name: 'ID' })).toBeNull();
    expect(screen.queryByText(/fax-wait|^sip$/)).toBeNull();
    expect(within(waiting).getAllByText('Waiting for the document')).toHaveLength(2);
    expect((within(waiting).getByRole('button', { name: `Download the fax from ${masked('+15550108888')}` }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(waiting).queryByRole('button', { name: /Retry delivery/ })).toBeNull();
    fireEvent.click(within(waiting).getByRole('button', { name: `Fetch again the fax from ${masked('+15550108888')}` }));
    expect(await screen.findByText('Faxbot will fetch the document shortly.')).toBeTruthy();
    expect(fetches).toEqual(['fax-waiting']);
    const test = await rowFor('+15550109999');
    expect(within(test).getByText('Test fax')).toBeTruthy();
    expect(within(test).queryByRole('button', { name: /Fetch again/ })).toBeNull();
  });

  it('offers Fetch again only to people who can change providers', async () => {
    server.use(
      http.get('/inbound', () => HttpResponse.json([{ ...fax('fax-failed', '+15550108888'), status: 'failed',
        can_fetch_again: true, status_text: 'Faxbot stopped trying to fetch this document; select Fetch again.' }])),
      http.get('/admin/inbound/callbacks', () => HttpResponse.json({ callbacks: [] })),
    );
    render(<Received client={client()} inboundEnabled permissions={operator} />);
    const failed = await rowFor('+15550108888');
    expect(within(failed).getByText('Not received')).toBeTruthy();
    expect(within(failed).getByText('Faxbot stopped trying to fetch this document; select Fetch again.')).toBeTruthy();
    expect(within(failed).queryByRole('button', { name: /Fetch again/ })).toBeNull();
  });
});

describe('Inbox wording for received faxes', () => {
  const READY = 'Received faxes reach Faxbot: ready.';

  function recoveredInbox(receiving: { ready: boolean; message: string }) {
    server.use(
      http.get('/inbound', () => HttpResponse.json([{ id: 'recovered', fr: null, to: null, status: 'received', backend: 'sip',
        pages: 1, received_at: '2026-10-04T03:48:00', source_received_at: '2026-10-04T03:14:00', recovered: true }])),
      http.get('/admin/inbound/callbacks', () => HttpResponse.json({ backend: 'sip', callbacks: [], receiving })),
      http.get('/intake/items', () => HttpResponse.json({ items: [], counts: { received: 0, sending: 0, delivered: 0, failed: 0 } })),
    );
  }

  it('says Unknown for a number nobody reported and shows when a recovered fax arrived', async () => {
    recoveredInbox({ ready: true, message: READY });
    render(<Received client={client()} inboundEnabled permissions={new Set([...operator, 'providers:read'])} />);
    const row = (await screen.findAllByText('Unknown'))[0].closest('tr') as HTMLElement;
    expect(within(row).getAllByText('Unknown')).toHaveLength(2);
    const arrived = formatServerTime('2026-10-04T03:14:00');
    expect(within(row).getByText(`${arrived} · brought in later`)).toBeTruthy();
    expect(within(row).queryByText(formatServerTime('2026-10-04T03:48:00'))).toBeNull();
    expect(screen.queryByText('****')).toBeNull();
  });

  it('shows one receiving line with a link to the trunk instead of a dialplan snippet', async () => {
    recoveredInbox({ ready: true, message: READY });
    const navigate = vi.fn();
    render(<Received client={client()} inboundEnabled onNavigate={navigate} permissions={new Set([...operator, 'providers:read'])} />);
    const status = await screen.findByTestId('sip-receiving');
    expect(status.textContent).toContain(READY);
    expect(screen.queryByText(/_internal|YOUR_SECRET|curl|dialplan/i)).toBeNull();
    fireEvent.click(within(status).getByRole('button', { name: 'Open Carrier trunk' }));
    expect(navigate).toHaveBeenCalledWith('trunk');
  });

  it('names a failed hand-over in that line', async () => {
    const sentence = 'A fax was received but could not be handed to Faxbot: Faxbot could not be reached.';
    recoveredInbox({ ready: false, message: sentence });
    render(<Received client={client()} inboundEnabled permissions={new Set([...operator, 'providers:read'])} />);
    expect((await screen.findByTestId('sip-receiving')).textContent).toContain(sentence);
  });

  it('keeps the time of day on a phone, so a recovered fax shows when it arrived', async () => {
    recoveredInbox({ ready: true, message: READY });
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({ ...original(query), matches: query.includes('max-width') })) as typeof window.matchMedia;
    try {
      render(<Received client={client()} inboundEnabled permissions={new Set([...operator, 'providers:read'])} />);
      const arrived = parseServerTime('2026-10-04T03:14:00')!
        .toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      expect(await screen.findByText(`${arrived} · brought in later`)).toBeTruthy();
    } finally {
      window.matchMedia = original;
    }
  });
});

describe('How received faxes reach Faxbot', () => {
  it('offers to bring in faxes the trunk did not hand over, to people who may change providers', async () => {
    const recovered: unknown[] = [];
    server.use(
      http.get('/admin/inbound/callbacks', () => HttpResponse.json({ backend: 'sip', callbacks: [],
        receiving: { ready: true, message: 'Received faxes reach Faxbot: ready.' } })),
      http.post('/admin/inbound/recover', () => {
        recovered.push(true);
        return HttpResponse.json({ found: 0, imported: 0, waiting: 0, message: 'No received faxes were waiting to be brought in.' });
      }),
    );
    const { unmount } = render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list', 'providers:read'])} />);
    expect(await screen.findByTestId('sip-receiving')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Bring in faxes that were received but not handed over' })).toBeNull();
    unmount();
    render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list', 'providers:read', 'providers:write'])} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Bring in faxes that were received but not handed over' }));
    expect(await screen.findByText('No received faxes were waiting to be brought in.')).toBeTruthy();
    expect(recovered).toHaveLength(1);
  });

  it('says when Faxbot last checked eFax, and any note about faxes still stored there', async () => {
    server.use(
      http.get('/admin/inbound/callbacks', () => HttpResponse.json({ backend: 'efax', callbacks: [] })),
      http.get('/admin/inbound/efax', () => HttpResponse.json({ receiving: true, checked_at: '2026-10-04T15:05:00', problem: null,
        pending_deletions: 1, stopped_deletions: 0, notes: ['Faxbot is still deleting 1 received fax from eFax.'] })),
    );
    render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list', 'providers:read'])} />);
    const line = await screen.findByTestId('efax-receiving');
    const at = parseServerTime('2026-10-04T15:05:00')!.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    expect(line.textContent).toBe(`Faxbot checks eFax for received faxes; it last checked at ${at}.`);
    expect(screen.getByText('Faxbot is still deleting 1 received fax from eFax.')).toBeTruthy();
  });

  it('says in one sentence where to turn receiving on when it is off', async () => {
    const navigate = vi.fn();
    render(<Received client={client()} inboundEnabled={false} onNavigate={navigate} permissions={new Set(['inbound:list', 'settings:read'])} />);
    expect(await screen.findByText('Receiving faxes is turned off. Turn it on under Providers, In use.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open Providers' }));
    expect(navigate).toHaveBeenCalledWith('providers/sending');
  });

  it('lists the addresses to give the receiving provider on In use', async () => {
    server.use(http.get('/admin/inbound/callbacks', () => HttpResponse.json({ backend: 'phaxio',
      callbacks: [{ name: 'Phaxio inbound', url: 'https://fax.example/phaxio-inbound' }] })));
    render(<ReceivingAddresses client={client()} />);
    expect(await screen.findByText('Addresses to give your provider')).toBeTruthy();
    expect(screen.getByText('https://fax.example/phaxio-inbound')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy();
  });
});
