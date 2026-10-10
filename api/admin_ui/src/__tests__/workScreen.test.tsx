// Received with owners: one sentence per state in local time, actions only where the server allows them.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { WorkItem } from '../api/types';
import Received from '../components/Received';
import { duplicateSentence, OPERATIONAL_TARGET, shortTime, targetLabel, workStateSentence } from '../components/work/text';
import { NAVIGATION, parseAddress, resolveAddress, visibleNavigation } from '../navigation';
import WorkSettingsPanel from '../components/work/WorkSettingsPanel';
import WorkDetail from '../components/work/WorkDetail';
import { formatServerTime } from '../api/time';
import type { IntakeItem } from '../api/deliveryTypes';
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


  it('shows Received to people who can read work, and the old Work address opens it waiting for an owner', () => {
    const permissions = new Set<string>();
    const areas = (inbox: boolean, work: boolean) => visibleNavigation(permissions, { send: false, jobs: false, inbox, work }, { pluginsEnabled: false });
    const faxPages = (inbox: boolean, work: boolean) => areas(inbox, work).find((area) => area.id === 'faxes')?.pages.map((page) => page.id);
    expect(faxPages(true, true)).toEqual(['received']);
    expect(faxPages(false, true)).toEqual(['received']);
    expect(faxPages(true, false)).toEqual(['received']);
    expect(faxPages(false, false)).toBeUndefined();
    expect(resolveAddress(areas(true, true), parseAddress('#/faxes/work'))?.address).toBe('#/faxes/received?show=waiting');
  });
});

function queue(items: WorkItem[], calls: Array<[string, unknown]> = [], views: Partial<Record<string, WorkItem[]>> = {}) {
  server.use(
    http.get('/work', ({ request }) => {
      const view = new URL(request.url).searchParams.get('view') ?? 'all';
      return HttpResponse.json({ items: views[view] ?? items });
    }),
    http.get('/work/counts', () => HttpResponse.json({ open: 2, acknowledged: 0, done: 0, unassigned: 1, mine: 1, overdue: 0 })),
    http.get('/inbound', () => HttpResponse.json([
      { id: 'late', fr: '+15550108888', to: '+15550100001', status: 'waiting', backend: 'phaxio', received_at: '2026-10-03T11:00:00',
        status_text: 'Waiting for the document from Phaxio.' },
      { id: 'fax-open', fr: '+15550109999', to: '+15550100001', status: 'received', backend: 'phaxio', received_at: '2026-10-03T12:00:00' },
      { id: 'fax-mine', fr: '+15550102222', to: '+15550100001', status: 'received', backend: 'import', received_at: '2026-10-03T12:05:00' },
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

const masked = (number: string) => '*'.repeat(number.length - 4) + number.slice(-4);
const rowFor = async (from: string) => (await screen.findByText(masked(from))).closest('tr') as HTMLElement;

const open = () => item({ id: 'open', inbound_fax_id: 'fax-open', actions: ['assign', 'done', 'export', 'document'], is_test: true });
const mine = () => item({ id: 'mine', inbound_fax_id: 'fax-mine', from_number: '+15550102222', state_key: 'assigned',
  state_text: 'Assigned to Dana.', owner: { id: 'dana', name: 'Dana' }, is_mine: true, actions: ['acknowledge'] });

describe('Received with owners (the former Work screen)', () => {
  it('offers only the actions the server allows, beside faxes still waiting for their document', async () => {
    queue([open(), mine()]);
    render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list'])} />);
    const openRow = await rowFor('+15550109999');
    const mineRow = await rowFor('+15550102222');
    expect(within(openRow).getByText('Waiting for an owner.')).toBeTruthy();
    expect(within(openRow).getByRole('button', { name: 'Assign' })).toBeTruthy();
    expect(within(openRow).getByText('Test fax')).toBeTruthy();
    expect(within(mineRow).queryByText('Test fax')).toBeNull();
    expect(within(openRow).getByRole('button', { name: 'Export' })).toBeTruthy();
    expect(within(openRow).queryByRole('button', { name: 'Acknowledge' })).toBeNull();
    expect(within(mineRow).getByRole('button', { name: 'Acknowledge' })).toBeTruthy();
    expect(within(mineRow).queryByRole('button', { name: 'Export' })).toBeNull();
    // An imported document is marked as imported.
    expect(within(mineRow).getByText('Imported')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Mine (1)' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Waiting for an owner (1)' })).toBeTruthy();
    const waiting = await rowFor('+15550108888');
    expect(within(waiting).getByText('Waiting for the document from Phaxio.')).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: 'Owner and status' })).toBeTruthy();
    expect(screen.queryByText('Acknowledgement targets')).toBeNull();  // no settings:read
    expect(screen.queryByText(/[0-9a-f]{32}/)).toBeNull();
  });

  it('marks an item done with a short note and the version it showed', async () => {
    const calls = queue([item({ id: 'open', inbound_fax_id: 'fax-open', version: 3, actions: ['done'] })]);
    render(<Received client={client()} inboundEnabled permissions={new Set()} />);
    fireEvent.click(within(await rowFor('+15550109999')).getByRole('button', { name: 'Done' }));
    fireEvent.change(screen.getByLabelText('What was done'), { target: { value: 'Filed.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Mark done' }));
    await waitFor(() => expect(calls).toEqual([['done', { note: 'Filed.', version: 3 }]]));
    expect(await screen.findByText('Done: Filed.')).toBeTruthy();
  });

  it('filters to faxes waiting for an owner, as the queue itself lists them, and keeps the filter in the address', async () => {
    queue([open(), mine()], [], { unassigned: [open()] });
    const shown: string[] = [];
    render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list'])} onShowChange={(next) => shown.push(next)} />);
    await rowFor('+15550102222');
    fireEvent.click(screen.getByRole('button', { name: 'Waiting for an owner (1)' }));
    await waitFor(() => expect(screen.queryByText(masked('+15550102222'))).toBeNull());
    expect(await rowFor('+15550109999')).toBeTruthy();
    expect(screen.queryByText(masked('+15550108888'))).toBeNull();
    expect(shown).toEqual(['waiting']);
  });

  it('reads as many work items as the server lists at once, so recent faxes keep their owner', async () => {
    queue([open()]);
    const asked: Array<string | null> = [];
    server.use(http.get('/work', ({ request }) => {
      const url = new URL(request.url);
      asked.push(`${url.searchParams.get('view')}:${url.searchParams.get('limit')}`);
      return HttpResponse.json({ items: [open()] });
    }));
    render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list'])} show="mine" />);
    await waitFor(() => expect(asked).toEqual(expect.arrayContaining(['all:200', 'mine:200'])));
  });

  it('opens on the filter the address names', async () => {
    queue([open(), mine()], [], { overdue: [] });
    render(<Received client={client()} inboundEnabled permissions={new Set(['inbound:list'])} show="overdue" />);
    expect(await screen.findByText('Nothing is overdue.')).toBeTruthy();
  });

  it('lists a document this person may work on even when it is not among the faxes they can list', async () => {
    queue([item({ id: 'elsewhere', inbound_fax_id: 'not-listed', from_number: '+15550104321', actions: ['assign'] })]);
    render(<Received client={client()} inboundEnabled permissions={new Set()} canList={false} />);
    const row = await rowFor('+15550104321');
    expect(within(row).getByRole('button', { name: 'Assign' })).toBeTruthy();
  });

  it('leaves acknowledgement targets to Numbers, Mailboxes', async () => {
    queue([]);
    render(<Received client={client()} inboundEnabled permissions={new Set(['settings:read'])} />);
    await rowFor('+15550109999');
    expect(screen.queryByText('Acknowledgement targets')).toBeNull();
  });
});

describe('Acknowledgement targets under Delivery setup, Mailboxes', () => {
  it('labels the target as operational, read only without settings:write', async () => {
    queue([]);
    const mailboxes = NAVIGATION.find((area) => area.id === 'delivery')!.pages.find((page) => page.id === 'mailboxes')!;
    expect(mailboxes.gate.anyOf).toContain('settings:read');
    render(<WorkSettingsPanel client={client()} canWrite={false} />);
    expect(await screen.findByText('Acknowledgement targets')).toBeTruthy();
    expect(screen.getByText(new RegExp(OPERATIONAL_TARGET.replace(/[.']/g, '.')))).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
  });
});

describe('the received fax detail', () => {
  const delivered = (overrides: Partial<IntakeItem>): IntakeItem => ({
    id: 'delivery', source: 'fax', inbound_fax_id: 'fax', received_at: '2026-10-03T12:00:00', pages: 1,
    from_number: '+15550109999', to_number: '+15550100001', state: 'delivered', status: 'Delivered.', needs_action: false,
    attempts: 1, next_attempt_at: null, delivered_at: '2026-10-03T12:00:09', connector: 'Front desk', ...overrides,
  });

  function open(delivery: IntakeItem) {
    const shown = item({});
    server.use(
      http.get('/work/:id', () => HttpResponse.json(shown)),
      http.get('/work/:id/history', () => HttpResponse.json({ events: [] })),
    );
    // The server's own sentence is in its installation's zone; the drawer builds it in the viewer's.
    const sentence = 'Failed once before Dana Lee asked Faxbot to fetch it again on 3 October 2026 at 2:00 PM UTC.';
    const earlier = [{ stopped_at: '2026-10-03T13:00:00', attempts: 30, problem: 'Phaxio did not answer.',
      resumed_at: '2026-10-03T14:00:00', resumed_by: 'person' as const, resumed_by_name: 'Dana Lee' }];
    render(<WorkDetail client={client()} item={shown} onClose={() => undefined} onDownload={() => undefined}
      fax={{ id: 'fax', status: 'received', backend: 'phaxio', earlier_failures: earlier, earlier_failures_text: sentence }}
      delivery={delivery} />);
    return within(screen.getByRole('dialog', { name: 'Work item' }));
  }

  it('names who the email went to from the delivery, and how often fetching stopped', () => {
    const drawer = open(delivered({ delivered_to: ['frontdesk@clinic.example'], recipients_recorded: true }));
    expect(drawer.getByText('Emailed to')).toBeTruthy();
    expect(drawer.getByText('frontdesk@clinic.example')).toBeTruthy();
    expect(drawer.getByText(
      `Failed once before Dana Lee asked Faxbot to fetch it again on ${formatServerTime('2026-10-03T14:00:00')}.`)).toBeTruthy();
  });

  it('says plainly when an older email delivery did not record who it went to', () => {
    const drawer = open(delivered({ delivered_to: [], recipients_recorded: false }));
    expect(drawer.getByText('Who it went to was not recorded when it was delivered.')).toBeTruthy();
  });
});
