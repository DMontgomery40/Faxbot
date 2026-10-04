// The Work screen: one sentence per state in local time, actions only where the server allows them.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { WorkItem } from '../api/types';
import Work from '../components/Work';
import { duplicateSentence, OPERATIONAL_TARGET, shortTime, targetLabel, workStateSentence } from '../components/work/text';
import { visibleTopTabs } from '../navigation';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const item = (overrides: Partial<WorkItem>): WorkItem => ({
  id: 'item', inbound_fax_id: 'fax', state: 'open', state_key: 'waiting', state_text: 'Waiting for an owner.',
  due_text: 'Acknowledge within 24 hours of the document arriving (installation setting)', due_at: null, due_hours: 24,
  due_source: 'installation', available_at: '2026-10-03T12:00:00', from_number: '+15550109999', to_number: '+15550100001',
  pages: 1, mailbox: 'Front Desk', owner: null, backup: null, assigned_at: null, acknowledged_by: null,
  acknowledged_at: null, escalated_at: null, done_at: null, done_by: null, done_note: null, duplicate_of: null,
  is_mine: false, overdue: false, version: 1, actions: [], ...overrides,
});

describe('work sentences', () => {
  it('shows the acknowledgement time in local time and keeps other sentences as written', () => {
    const assigned = item({ state_key: 'assigned', state_text: 'Assigned to Dana; acknowledge by 4 Oct 12:00 UTC.',
      due_at: '2026-10-04T12:00:00', owner: { id: 'dana', name: 'Dana' } });
    expect(workStateSentence(assigned)).toBe(`Assigned to Dana; acknowledge by ${shortTime('2026-10-04T12:00:00')}.`);
    expect(workStateSentence(assigned)).not.toContain('UTC');
    expect(workStateSentence(item({ state_key: 'escalated', state_text: 'Overdue; escalated to Sam.' })))
      .toBe('Overdue; escalated to Sam.');
    expect(workStateSentence(item({ state_key: 'done', state_text: 'Done: filed in the case system.' })))
      .toBe('Done: filed in the case system.');
    expect(duplicateSentence(item({ duplicate_of: { id: 'other', available_at: '2026-10-03T09:00:00' } })))
      .toBe(`Same document as the one received ${shortTime('2026-10-03T09:00:00')}.`);
    expect(duplicateSentence(item({}))).toBeNull();
    expect([targetLabel(null), targetLabel(0), targetLabel(1), targetLabel(8)])
      .toEqual(['Installation target', 'No target', '1 hour', '8 hours']);
  });

  it('places Work between Inbox and Settings only for people who can read work', () => {
    const permissions = new Set<string>();
    expect(visibleTopTabs(permissions, { send: false, jobs: false, inbox: true, work: true }, false))
      .toEqual(['inbox', 'work', 'settings']);
    expect(visibleTopTabs(permissions, { send: false, jobs: false, inbox: true, work: false }, false))
      .toEqual(['inbox', 'settings']);
  });
});

function queue(items: WorkItem[], calls: Array<[string, unknown]> = []) {
  server.use(
    http.get('/work', () => HttpResponse.json({ items })),
    http.get('/work/counts', () => HttpResponse.json({ open: 2, acknowledged: 0, done: 0, unassigned: 1, mine: 1, overdue: 0 })),
    http.get('/inbound', () => HttpResponse.json([
      { id: 'late', fr: '+15550108888', to: '+15550100001', status: 'waiting', backend: 'phaxio', received_at: '2026-10-03T11:00:00',
        status_text: 'Waiting for the document from Phaxio.' },
      { id: 'here', fr: '+15550107777', to: '+15550100001', status: 'received', backend: 'phaxio', received_at: '2026-10-03T11:00:00' },
    ])),
    http.get('/work/settings', () => HttpResponse.json({ acknowledge_hours: 24, mailboxes: [
      { mailbox_id: 'front', label: 'Front Desk', enabled: true, acknowledge_hours: null, backup: null, version: 0,
        people: [{ id: 'sam', name: 'Sam', login: 'sam' }] },
    ] })),
    http.get('/work/:id/assignees', () => HttpResponse.json({ people: [{ id: 'dana', name: 'Dana', login: 'dana' }] })),
    http.post('/work/:id/:action', async ({ params, request }) => {
      calls.push([`${params.action}`, await request.json()]);
      return HttpResponse.json(item({ id: String(params.id), state: 'done', state_key: 'done', state_text: 'Done: Filed.' }));
    }),
  );
  return calls;
}

describe('Work screen', () => {
  it('offers only the actions the server allows and lists documents still waiting', async () => {
    queue([
      item({ id: 'open', actions: ['assign', 'done', 'export', 'document'] }),
      item({ id: 'mine', from_number: '+15550102222', state_key: 'assigned', state_text: 'Assigned to Dana.',
        owner: { id: 'dana', name: 'Dana' }, is_mine: true, actions: ['acknowledge'] }),
    ]);
    render(<Work client={client()} permissions={new Set(['inbound:list'])} />);
    const table = await screen.findByRole('table', { name: 'Work items' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(within(rows[0]).getByRole('button', { name: 'Assign' })).toBeTruthy();
    expect(within(rows[0]).getByRole('button', { name: 'Export' })).toBeTruthy();
    expect(within(rows[0]).queryByRole('button', { name: 'Acknowledge' })).toBeNull();
    expect(within(rows[1]).getByRole('button', { name: 'Acknowledge' })).toBeTruthy();
    expect(within(rows[1]).queryByRole('button', { name: 'Export' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Mine (1)' })).toBeTruthy();
    const waiting = screen.getByRole('table', { name: 'Waiting for documents' });
    expect(within(waiting).getAllByRole('row')).toHaveLength(1);
    expect(within(waiting).getByText('Waiting for the document from Phaxio.')).toBeTruthy();
    expect(screen.queryByText('Acknowledgement targets')).toBeNull();  // no settings:read
    expect(screen.queryByText(/[0-9a-f]{32}/)).toBeNull();
  });

  it('marks an item done with a short note and the version it showed', async () => {
    const calls = queue([item({ id: 'open', version: 3, actions: ['done'] })]);
    render(<Work client={client()} permissions={new Set()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }));
    fireEvent.change(screen.getByLabelText('What was done'), { target: { value: 'Filed.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Mark done' }));
    await waitFor(() => expect(calls).toEqual([['done', { note: 'Filed.', version: 3 }]]));
    expect(await screen.findByText('Done: Filed.')).toBeTruthy();
  });

  it('labels the acknowledgement target as operational for people who can read settings', async () => {
    queue([]);
    render(<Work client={client()} permissions={new Set(['settings:read'])} />);
    expect(await screen.findByText('Acknowledgement targets')).toBeTruthy();
    expect(screen.getByText(new RegExp(OPERATIONAL_TARGET.replace(/[.']/g, '.')))).toBeTruthy();
    expect(screen.getByText('No received documents yet.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();  // read only without settings:write
  });
});
