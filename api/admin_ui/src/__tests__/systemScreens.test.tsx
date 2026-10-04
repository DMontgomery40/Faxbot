// System, milestone 5: the Audit log screen, database status in Diagnostics, Logs in plain
// column names, the terminal's environment-only switch and an empty case list. Synthetic data only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import AuditLog, { auditAction } from '../components/AuditLog';
import DatabaseStatus from '../components/DatabaseStatus';
import Logs, { parseQueryTokens } from '../components/Logs';
import CasePackets from '../components/delivery/CasePackets';
import { DeploymentSection } from '../components/common/Deployment';
import { server } from '../test/server';
import { settingsFixture } from '../test/settingsFixture';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const entry = (id: string, operation: string, extra: Record<string, unknown> = {}) => ({
  id, at: '2026-10-04T15:00:00', actor: { id: 'p_front', display_name: 'Front desk' }, credential_kind: 'session',
  operation, target: { kind: 'mailbox', id: 'mbx_1', name: 'Billing' }, outcome: 'allowed', policy_version: 3, details: {},
  ...extra,
});

describe('Audit log', () => {
  it('lists who did what, newest first, in plain words, and loads older entries', async () => {
    const asked: string[] = [];
    server.use(http.get('/access/audit', ({ request }) => {
      const url = new URL(request.url);
      asked.push(url.search);
      if (url.searchParams.get('cursor') === 'older') {
        return HttpResponse.json({ items: [entry('a3', 'issue_password_session', { target: null })], next_cursor: null });
      }
      return HttpResponse.json({ items: [
        entry('a1', 'update_mailbox'),
        entry('a2', 'settings.update', { actor: null, credential_kind: 'system', outcome: 'denied',
          target: { kind: 'installation', id: 'installation', name: null } }),
      ], next_cursor: 'older' });
    }));
    render(<AuditLog client={client()} canListPeople={false} />);
    const table = await screen.findByRole('table', { name: 'Audit log' });
    const first = (await within(table).findByText('Changed a mailbox')).closest('tr') as HTMLElement;
    expect(within(first).getByText('Front desk')).toBeTruthy();
    expect(within(first).getByText('Signed in')).toBeTruthy();
    expect(within(first).getByText('Mailbox: Billing')).toBeTruthy();
    expect(within(first).getByText('Done')).toBeTruthy();
    const second = within(table).getByText('Changed settings').closest('tr') as HTMLElement;
    expect(within(second).getAllByText('Faxbot').length).toBeGreaterThan(0);
    expect(within(second).getByText('This installation')).toBeTruthy();
    expect(within(second).getByText('Refused')).toBeTruthy();
    expect(within(table).queryByText(/update_mailbox|settings\.update/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show older entries' }));
    expect(await within(table).findByText('Signed in with a password')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Show older entries' })).toBeNull();
    expect(asked[1]).toContain('cursor=older');
  });

  it('filters by person and by action on the server', async () => {
    const asked: URLSearchParams[] = [];
    server.use(
      http.get('/access/audit', ({ request }) => {
        asked.push(new URL(request.url).searchParams);
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
      http.get('/access/users', () => HttpResponse.json({ items: [{ id: 'p_front', display_name: 'Front desk', kind: 'user' }],
        next_cursor: null })),
    );
    render(<AuditLog client={client()} canListPeople />);
    expect(await screen.findByText('Nothing matches these filters.')).toBeTruthy();
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Action' }));
    fireEvent.click(await screen.findByRole('option', { name: 'Revoked an API key' }));
    await waitFor(() => expect(asked[asked.length - 1]?.get('operation')).toBe('revoke_key'));
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Who' }));
    fireEvent.click(await screen.findByRole('option', { name: 'Front desk' }));
    await waitFor(() => expect(asked[asked.length - 1]?.get('actor_id')).toBe('p_front'));
  });

  it('reads an action it does not know as words rather than a code', () => {
    expect(auditAction('host.restart')).toBe('Restarted Faxbot');
    expect(auditAction('new_thing.happened')).toBe('New thing happened');
  });
});

describe('Database status in Diagnostics', () => {
  it('says what kind of database Faxbot uses, that it can reach it, and what it holds', async () => {
    server.use(http.get('/admin/db-status', () => HttpResponse.json({ url: 'sqlite:////faxdata/faxbot.db', engine: 'sqlite',
      connected: true, error: null, counts: { fax_jobs: 12, inbound_fax: 4, api_keys: 2 },
      sqlite: { path: '/faxdata/faxbot.db', exists: true, size_bytes: 2_516_582, modified: '2026-10-04T15:00:00', persistent_volume: true } })));
    render(<DatabaseStatus client={client()} />);
    const card = await screen.findByTestId('database-status');
    expect(await within(card).findByText('Kind: A database file on this server')).toBeTruthy();
    expect(within(card).getByText('Faxbot can reach its database.')).toBeTruthy();
    expect(within(card).getByText('You can see 12 sent faxes, 4 received faxes and 2 API keys.')).toBeTruthy();
    expect(within(card).getByText(/^File: \/faxdata\/faxbot\.db, 2\.4 MB, last changed /)).toBeTruthy();
    expect(within(card).queryByText(/sqlite:/)).toBeNull();
  });

  it('warns about a file outside the data folder and about a database it cannot reach', async () => {
    server.use(http.get('/admin/db-status', () => HttpResponse.json({ url: 'postgresql://***', engine: 'postgres',
      connected: false, error: 'connection refused', counts: {}, sqlite: null })));
    const { unmount } = render(<DatabaseStatus client={client()} />);
    expect(await screen.findByText('Kind: PostgreSQL')).toBeTruthy();
    expect(screen.getByText('Faxbot cannot reach its database.')).toBeTruthy();
    expect(screen.getByText('Details: connection refused')).toBeTruthy();
    unmount();
    server.use(http.get('/admin/db-status', () => HttpResponse.json({ url: 'sqlite:///./faxbot.db', engine: 'sqlite',
      connected: true, error: null, counts: { fax_jobs: 0, inbound_fax: 0, api_keys: null },
      sqlite: { path: '/app/faxbot.db', exists: true, size_bytes: 512, persistent_volume: false } })));
    render(<DatabaseStatus client={client()} />);
    expect(await screen.findByText("This file is not in Faxbot's data folder, so it could be lost when Faxbot is reinstalled.")).toBeTruthy();
    expect(screen.getByText('You can see 0 sent faxes and 0 received faxes.')).toBeTruthy();
  });
});

describe('Logs in plain words', () => {
  it('names each column plainly and searches one column by its plain name', async () => {
    server.use(http.get('/admin/logs', () => HttpResponse.json({ items: [
      { ts: '2026-10-04T15:00:00Z', event: 'job_failed', job_id: 'job-1', key_id: 'k1', backend: 'sinch', status: 'failed',
        error: 'Busy', to: '+15555550123', from: '+15555550100', path: '/fax' },
      { ts: '2026-10-04T15:01:00Z', event: 'job_sent', job_id: 'job-2', backend: 'phaxio', status: 'success' },
    ], count: 2 })), http.get('/admin/settings', () => HttpResponse.json(settingsFixture())));
    render(<Logs client={client()} />);
    expect(await screen.findByText('job_failed')).toBeTruthy();
    const headers = screen.getAllByRole('columnheader').map((cell) => cell.textContent);
    expect(headers).toEqual(['Time', 'Event', 'Fax', 'Key', 'Provider', 'Result', 'Error', 'To', 'From']);
    expect(screen.queryByText(/key:value/)).toBeNull();
    expect(screen.getByText(/To search one column, type its name, a colon and the words/)).toBeTruthy();
    expect(parseQueryTokens('provider:sinch result:failed busy')).toEqual({ q: 'busy', filters: { backend: 'sinch', status: 'failed' } });
    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'provider:sinch' } });
    fireEvent.click(screen.getByRole('button', { name: 'Show matching entries' }));
    await waitFor(() => expect(screen.queryByText('job_sent')).toBeNull());
    expect(screen.getByText('job_failed')).toBeTruthy();
  });
});

describe('The terminal switch and an empty case list', () => {
  it('shows whether the terminal is on, set where Faxbot is installed', async () => {
    const data = settingsFixture((value) => {
      value.deployment = { ENABLE_ADMIN_EXEC: { set: false, value: null, effective: true } };
    });
    server.use(http.get('/admin/settings', () => HttpResponse.json(data)));
    render(<DeploymentSection client={client()} names={['ENABLE_ADMIN_EXEC']} title="Terminal setting" showNames />);
    expect(await screen.findByText('Terminal is on')).toBeTruthy();
    expect(screen.getByDisplayValue('On')).toBeTruthy();
    expect(screen.getByText('Not set: the terminal is on when this installation serves the console. (ENABLE_ADMIN_EXEC)')).toBeTruthy();
  });

  it('says so in one sentence when no case packet was ever sent', async () => {
    render(<CasePackets client={client()} canSend={false} canWrite={false} />);
    expect(await screen.findByText('No case packets have been sent yet.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Look up' })).toBeTruthy();
  });
});

describe('Audit log one-time codes', () => {
  it('names a terminal access code and a phone pairing code', async () => {
    server.use(http.get('/access/audit', () => HttpResponse.json({ items: [
      entry('c1', 'capability.issue', { target: { kind: 'capability', id: 'cap-1', name: null }, details: { kind: 'terminal' } }),
      entry('c2', 'capability.consume', { target: { kind: 'capability', id: 'cap-2', name: null }, details: { kind: 'pairing' } }),
    ], next_cursor: null })));
    render(<AuditLog client={client()} canListPeople={false} />);
    expect(await screen.findByText('Terminal access code')).toBeTruthy();
    expect(screen.getByText('Phone pairing code')).toBeTruthy();
    expect(screen.queryByText('Item')).toBeNull();
  });
});
