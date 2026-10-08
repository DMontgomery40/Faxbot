// Numbers → Email and folders: connectors with their status and counts, add, test, pause and recent items.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Connectors, { FaxRequestedByItem } from '../components/delivery/Connectors';
import type { Connector } from '../api/connectorTypes';
import { NAVIGATION } from '../navigation';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const connector = (overrides: Partial<Connector>): Connector => ({
  id: 'c1', name: 'Email to fax', kind: 'email', direction: 'send', what: 'Faxes what people email', enabled: true,
  paused: false, status: 'Nothing new.', ok: true, last_checked_at: '2026-10-07T15:00:00', has_secret: true,
  has_sending_key: true, sending_key_id: 'abcdef012345', settings: { provider: 'google', address: 'fax@example.com' },
  version: 1, created_at: '2026-10-07T14:00:00', counts: { items: 4, duplicates: 3, refused: 1, failed: 0 },
  senders: [{ address: 'jane@example.com', principal_id: 'p1', name: 'Jane Smith', enabled: true }], ...overrides,
});

const choices = {
  mailboxes: [{ id: 'm1', label: 'Front Desk', number: '+15550100001' }, { id: 'm2', label: 'Billing', number: null }],
  people: [{ id: 'p1', name: 'Jane Smith', login: 'jane' }],
  providers: [
    { id: 'microsoft365', guidance: 'Microsoft 365 turned off passwords for IMAP in 2022.', sign_in: 'microsoft_app',
      checks_senders_with: 'Microsoft 365' },
    { id: 'google', guidance: 'Google Workspace stopped accepting plain passwords on March 14, 2025.',
      sign_in: 'google_service_account', checks_senders_with: 'mx.google.com' },
    { id: 'other', guidance: 'Enter your mail server\'s IMAP address and a user name and password.', sign_in: 'password' },
  ],
};

describe('Email and folders', () => {
  it('is a Numbers page for people who read settings', () => {
    const numbers = NAVIGATION.find((area) => area.id === 'numbers')!;
    const page = numbers.pages.find((item) => item.id === 'connectors')!;
    expect(page.label).toBe('Email and folders');
    expect(page.gate).toEqual({ anyOf: ['settings:read'] });
  });

  it('lists connectors with their status, counts, senders and recent items', async () => {
    server.use(
      http.get('/intake/sources', () => HttpResponse.json({ connectors: [
        connector({}),
        connector({ id: 'c2', name: 'Scanner share', kind: 'folder', direction: 'receive', what: 'Brings in documents from a folder',
          paused: true, enabled: false, status: 'Paused by Jane Smith.', has_sending_key: false, senders: undefined,
          mailbox: { id: 'm1', label: 'Front Desk', number: '+15550100001' }, counts: { items: 2, duplicates: 0, refused: 0, failed: 0 } }),
      ] })),
      http.get('/intake/sources/items', ({ request }) => {
        expect(new URL(request.url).searchParams.get('connector')).toBe('c1');
        return HttpResponse.json({ items: [{ id: 'i1', connector_id: 'c1', connector: 'Email to fax', direction: 'send',
          state: 'refused', status: 'Not sent: bob@example.com is not one of the people this connector may send faxes for.',
          what: '+13035550100', sender: 'bob@example.com', sender_name: null, to_number: null, duplicates: 0, refused: true,
          fax_id: null, inbound_id: null, reply: 'The sender was told by email.', reply_detail: null, received_at: null,
          created_at: '2026-10-07T15:00:00' }] });
      }),
    );
    render(<Connectors client={client()} canWrite />);
    expect(await screen.findByText('Email to fax')).toBeTruthy();
    expect(screen.getByText('4 handled · 3 seen again and never handled twice · 1 refused')).toBeTruthy();
    expect(screen.getByText('May send: Jane Smith (jane@example.com)')).toBeTruthy();
    expect(screen.getByText('Documents go to Front Desk.')).toBeTruthy();
    expect(screen.getByText(/^Paused by Jane Smith. Last checked /)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
    fireEvent.click(screen.getAllByRole('button', { name: 'Recent' })[0]);
    expect(await screen.findByText(/is not one of the people this connector may send faxes for/)).toBeTruthy();
    expect(screen.getByText('The sender was told by email.')).toBeTruthy();
  });

  it('adds a connector that faxes what people email, with the service guidance and its senders', async () => {
    let created: Record<string, unknown> | null = null;
    server.use(
      http.get('/intake/sources', () => HttpResponse.json({ connectors: [] })),
      http.get('/intake/sources/choices', () => HttpResponse.json(choices)),
      http.post('/intake/sources', async ({ request }) => {
        created = await request.json() as Record<string, unknown>;
        return HttpResponse.json(connector({}), { status: 201 });
      }),
    );
    render(<Connectors client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a connector' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Email to fax' } });
    fireEvent.change(within(dialog).getByLabelText('What it does'), { target: { value: 'mail-out' } });
    fireEvent.change(within(dialog).getByLabelText('Mail service'), { target: { value: 'google' } });
    expect(within(dialog).getByText(/stopped accepting plain passwords on March 14, 2025/)).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Mailbox address'), { target: { value: 'fax@example.com' } });
    fireEvent.change(within(dialog).getByLabelText('Service account key'), { target: { value: '{"type":"service_account"}' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add a person' }));
    fireEvent.change(within(dialog).getByLabelText('Email address'), { target: { value: 'jane@example.com' } });
    fireEvent.change(within(dialog).getByLabelText('Faxbot person'), { target: { value: 'p1' } });
    expect(within(dialog).getByText(/its own key that may only send faxes/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({ name: 'Email to fax', kind: 'email', direction: 'send',
      settings: { provider: 'google', address: 'fax@example.com', sign_in: 'google_service_account' },
      secret: { service_account: '{"type":"service_account"}' },
      senders: [{ address: 'jane@example.com', principal_id: 'p1' }] });
    expect(await screen.findByText('Connector saved. Use Test to check it.')).toBeTruthy();
  });

  it('files a folder straight into any mailbox, with or without a fax number, and says when you cannot', async () => {
    let created: Record<string, unknown> | null = null;
    server.use(
      http.get('/intake/sources', () => HttpResponse.json({ connectors: [] })),
      http.get('/intake/sources/choices', () => HttpResponse.json(choices)),
      http.post('/intake/sources', async ({ request }) => {
        created = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ detail: "You can’t see faxes in Billing, so a connector you set up can’t file into "
          + 'it. Choose a mailbox whose faxes you can see.' }, { status: 403 });
      }),
    );
    render(<Connectors client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a connector' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Scanner share' } });
    fireEvent.change(within(dialog).getByLabelText('What it does'), { target: { value: 'folder-in' } });
    fireEvent.change(within(dialog).getByLabelText('Folder'), { target: { value: '/scans' } });
    const box = within(dialog).getByLabelText('Mailbox') as HTMLSelectElement;
    expect(Array.from(box.options).map((option) => option.textContent)).toEqual([
      'None: every document needs a sidecar file', 'Front Desk (+15550100001)', 'Billing']);
    fireEvent.change(box, { target: { value: 'm2' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({ kind: 'folder', direction: 'receive', settings: { path: '/scans', mailbox_id: 'm2' } });
    expect(await within(dialog).findByText(/You can’t see faxes in Billing/)).toBeTruthy();
  });

  it('tests, pauses and removes a connector, and says why a test failed', async () => {
    const calls: string[] = [];
    server.use(
      http.get('/intake/sources', () => HttpResponse.json({ connectors: [connector({})] })),
      http.post('/intake/sources/:id/test', () => HttpResponse.json({ ok: false,
        detail: 'The mail server did not accept the user name and password.' })),
      http.post('/intake/sources/:id/pause', ({ params }) => { calls.push(`pause ${params.id}`); return HttpResponse.json(connector({ paused: true })); }),
      http.delete('/intake/sources/:id', ({ params }) => { calls.push(`remove ${params.id}`); return HttpResponse.json({ removed: true }); }),
    );
    render(<Connectors client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Test' }));
    expect(await screen.findByText('The mail server did not accept the user name and password.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(await screen.findByText('Email to fax is paused.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    const confirm = await screen.findByRole('dialog');
    expect(within(confirm).getByText(/revokes its sending key/)).toBeTruthy();
    fireEvent.click(within(confirm).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(calls).toEqual(['pause c1', 'remove c1']));
  });

  it('hides the page for people who cannot read settings, and shows who asked for a sent fax', async () => {
    server.use(
      http.get('/intake/sources', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })),
      http.get('/intake/sources/faxes/:id', ({ params }) => (params.id === 'job-1'
        ? HttpResponse.json({ fax_id: 'job-1', connector: 'Email to fax', person: 'Jane Smith', address: 'jane@example.com',
          sentence: 'Sent by email from Jane Smith (jane@example.com) through Email to fax.' })
        : HttpResponse.json({ detail: 'This fax did not come from a connector.' }, { status: 404 }))),
    );
    const { container } = render(<Connectors client={client()} canWrite={false} />);
    await waitFor(() => expect(container.innerHTML).toBe(""));
    render(<FaxRequestedByItem client={client()} jobId="job-1" />);
    expect(await screen.findByText('Sent by email from Jane Smith (jane@example.com) through Email to fax.')).toBeTruthy();
    const other = render(<FaxRequestedByItem client={client()} jobId="job-2" />);
    await waitFor(() => expect(other.container.innerHTML).toBe(""));
  });

  it('says when the connectors could not load instead of hiding the page', async () => {
    server.use(http.get('/intake/sources', () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })));
    render(<Connectors client={client()} canWrite />);
    expect(await screen.findByText('This item no longer exists. Reload and try again.')).toBeTruthy();
  });
});
