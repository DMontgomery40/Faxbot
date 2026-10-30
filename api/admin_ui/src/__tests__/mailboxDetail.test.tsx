import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { fireEvent, render, screen, within } from '@testing-library/react';
import AdminAPIClient from '../api/client';
import MailboxDetail from '../components/MailboxDetail';
import { backend, server } from '../test/server';

async function show(id: string, settings = false) {
  if (settings) {
    const admin = backend.state.principals.get('p_admin')!;
    admin.permissions = [...admin.permissions, 'settings:read'];
  }
  await AdminAPIClient.login('admin', 'correct horse');
  const client = new AdminAPIClient({ kind: 'session', csrf: null });
  const me = await client.me();
  render(<MailboxDetail client={client} me={me} mailboxId={id} currency="USD" onNavigate={() => undefined} />);
}

describe('mailbox details', () => {
  it('loads a copied address with scoped editors and an installation-wide fallback', async () => {
    backend.state.mailboxes.set('other', { id: 'other', label: 'Other mailbox', enabled: true,
      resource_id: 'res_other', version: 1 });
    backend.state.rules.set('r1', { id: 'r1', to_number: '+15550100001', mailbox_id: 'mbx_main', version: 1 });
    backend.state.rules.set('r2', { id: 'r2', to_number: '+15550100002', mailbox_id: 'other', version: 1 });
    await show('mbx_main', true);
    expect(await screen.findByRole('heading', { name: 'Main line' })).toBeTruthy();
    expect(await screen.findByRole('cell', { name: '+15550100001' })).toBeTruthy();
    expect(screen.queryByRole('cell', { name: '+15550100002' })).toBeNull();
    expect(await screen.findByRole('region', { name: 'Sending rules for Main line' })).toBeTruthy();
    expect(await screen.findByLabelText('Installation target (hours)')).toBeTruthy();
    expect(screen.getByText(/Installation-wide target/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'All mailboxes' }).getAttribute('href')).toBe('#/delivery/mailboxes');
    expect(await screen.findByText(/Your organization has no published rules/)).toBeTruthy();
  });

  it('refreshes the name after saving without changing the address', async () => {
    await show('mbx_main');
    fireEvent.click(await screen.findByRole('button', { name: 'Rename Main line' }));
    const dialog = await screen.findByRole('dialog', { name: 'Rename mailbox' });
    fireEvent.change(within(dialog).getByLabelText('Mailbox name'), { target: { value: 'Front desk' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('heading', { name: 'Front desk' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Main line' })).toBeNull();
    expect(screen.queryByLabelText('Installation target (hours)')).toBeNull();
  });

  it('keeps permission failures visible and can retry after access changes', async () => {
    server.use(http.get('/access/mailboxes', () => HttpResponse.json({ detail: 'Denied' }, { status: 403 })));
    await show('mbx_main');
    const retry = await screen.findByRole('button', { name: 'Retry' });
    expect(screen.queryByRole('button', { name: 'Rename Main line' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Main line' })).toBeNull();
    server.resetHandlers();
    fireEvent.click(retry);
    expect(await screen.findByRole('heading', { name: 'Main line' })).toBeTruthy();
  });

  it('finds a copied mailbox and its mappings beyond the first result page', async () => {
    server.use(
      http.get('/access/mailboxes', ({ request }) => HttpResponse.json(
        new URL(request.url).searchParams.get('cursor') === 'next'
          ? { items: [{ ...backend.state.mailboxes.get('mbx_main')!, rule_count: 1 }], next_cursor: null }
          : { items: [], next_cursor: 'next' })),
      http.get('/access/inbound-rules', ({ request }) => HttpResponse.json(
        new URL(request.url).searchParams.get('cursor') === 'next'
          ? { items: [{ id: 'r1', to_number: '+15550100001', mailbox_id: 'mbx_main',
            mailbox_label: 'Main line', version: 1 }], next_cursor: null }
          : { items: [], next_cursor: 'next' })),
    );
    await show('mbx_main');
    expect(await screen.findByRole('heading', { name: 'Main line' })).toBeTruthy();
    expect(await screen.findByRole('cell', { name: '+15550100001' })).toBeTruthy();
  });

  it('does not render editors for an unknown mailbox', async () => {
    await show('missing');
    expect(await screen.findByText('Mailbox not found or no longer accessible.')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Sending rules' })).toBeNull();
    expect(backend.requestsTo('GET', '/work/settings')).toHaveLength(0);
  });
});
