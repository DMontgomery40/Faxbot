// Faxes → Expected: sentences in local time, proposals confirmed with the version shown, the import with its
// saved column mapping, and the three outage lists. No internal ids on screen.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { ExpectedFax, ImportRun, Outage } from '../api/expectedTypes';
import ExpectedFaxes, { countsSentence, expectedStateSentence } from '../components/expected/ExpectedFaxes';
import { maskNumber, shortTime } from '../components/work/text';
import { NAVIGATION, visibleNavigation } from '../navigation';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const expected = (overrides: Partial<ExpectedFax> = {}): ExpectedFax => ({
  id: 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4', code: 'ACD347', reference: 'PO 483', kind: 'Signed acknowledgement',
  description: null, required_parts: [], required_revision: null, counterparty: 'Acme Supply',
  fax_numbers: ['+15550104444'], direct_address: null, mailbox: 'Purchasing', mailbox_id: 'box-1', owner: null,
  state: 'open', state_key: 'waiting', state_text: 'Waiting; expected by 5 Oct 2:00 PM UTC.',
  due_at: '2026-10-05T14:00:00', due_text: 'Expected within 48 hours, from the due time given when it was added. '
    + 'This is an operational target, not a legal deadline.', overdue: false, source: null, operation_id: null,
  revision: null, replaces: null, missing_from_export: false, conflict: false, matched_at: null, match: null,
  also_arrived: 0, proposals: [], keys: { subaddress: '483', email_subject: null, message_id: null, form_field: null },
  created_at: '2026-10-03T12:00:00', version: 2, actions: ['cancel', 'completed_elsewhere', 'match', 'export'],
  ...overrides,
});

const proposed = () => expected({
  state: 'proposed_match', state_key: 'proposed', state_text: 'A received fax may be the one; confirm or reject it.',
  proposals: [{ id: 'link-1', signal: 'counterparty_number', strength: 'weak', version: 1, can_decide: true,
    text: 'It came from a fax number you listed for Acme Supply, but nothing in it names PO 483.',
    fax: { inbound_fax_id: 'fax-1', from_number: '+15550104444', to_number: '+15550100001', pages: 2,
      received_at: '2026-10-03T13:00:00', mailbox: 'Purchasing' } }],
});

function serve(items: ExpectedFax[], calls: Array<[string, unknown]>) {
  server.use(
    http.get('/expected-faxes', () => HttpResponse.json({ expected: items })),
    http.get('/expected-faxes/counts', () => HttpResponse.json(
      { waiting: items.length, overdue: 0, proposed: 1, missing: 0, conflicts: 0, matched: 0 })),
    http.get('/expected-faxes/:code', () => HttpResponse.json(items[0])),
    http.get('/expected-faxes/:code/history', () => HttpResponse.json({ events: [
      { kind: 'created', occurred_at: '2026-10-03T12:00:00', actor: 'Dana', source: 'person', text: 'Dana added it.' },
    ] })),
    http.post('/expected-faxes/:code/:action', async ({ params, request }) => {
      calls.push([`${params.action}`, await request.json()]);
      return HttpResponse.json(expected({ state: 'matched', state_key: 'matched', state_text: 'Matched by Dana.' }));
    }),
  );
}

describe('expected fax sentences', () => {
  it('writes the due time in local time and keeps other sentences as the server wrote them', () => {
    expect(expectedStateSentence(expected())).toBe(`Waiting; expected by ${shortTime('2026-10-05T14:00:00')}.`);
    expect(expectedStateSentence(expected())).not.toContain('UTC');
    expect(expectedStateSentence(proposed())).toBe('A received fax may be the one; confirm or reject it.');
    expect(countsSentence({ waiting: 1, overdue: 0, proposed: 1, missing: 0, conflicts: 0, matched: 3 }))
      .toBe('1 expected fax is waiting; 0 overdue, 1 to confirm.');
  });

  it('is a Faxes page for people who read work or import it', () => {
    const faxes = (permissions: string[]) => visibleNavigation(new Set(permissions),
      { send: false, jobs: false, inbox: false, work: false }, { pluginsEnabled: false })
      .find((area) => area.id === 'faxes')?.pages.map((page) => page.id) ?? [];
    expect(faxes(['work:read'])).toContain('expected');
    expect(faxes(['work:import'])).toContain('expected');
    expect(faxes([])).not.toContain('expected');
    expect(NAVIGATION.find((area) => area.id === 'faxes')?.pages.map((page) => page.id)).toContain('expected');
  });
});

describe('Faxes → Expected', () => {
  it('lists expected faxes and confirms a proposed fax with the version it showed', async () => {
    const calls: Array<[string, unknown]> = [];
    serve([proposed()], calls);
    render(<ExpectedFaxes client={client()} canImport={false} canOutage={false} />);
    expect(await screen.findByText('1 expected fax is waiting; 0 overdue, 1 to confirm.')).toBeTruthy();
    expect(screen.queryByRole('tab', { name: 'Import' })).toBeNull();
    const row = (await screen.findByText('PO 483')).closest('tr') as HTMLElement;
    expect(within(row).getByText('To confirm')).toBeTruthy();
    fireEvent.click(row);
    expect(await screen.findByText('It came from a fax number you listed for Acme Supply, but nothing in it names PO 483.'))
      .toBeTruthy();
    expect(screen.getByText(`From ${maskNumber('+15550104444')}, received ${shortTime('2026-10-03T13:00:00')} in Purchasing.`))
      .toBeTruthy();
    expect(screen.getByText('Dana added it.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'This is it' }));
    await waitFor(() => expect(calls).toEqual([['confirm', { proposal_id: 'link-1', version: 2 }]]));
    expect(screen.queryByText(/[0-9a-f]{32}/)).toBeNull();
  });

  it('cancels with a note', async () => {
    const calls: Array<[string, unknown]> = [];
    serve([expected()], calls);
    render(<ExpectedFaxes client={client()} canImport={false} canOutage={false} />);
    fireEvent.click((await screen.findByText('PO 483')).closest('tr') as HTMLElement);
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel it' }));
    fireEvent.change(screen.getByLabelText('Why it is no longer expected'), { target: { value: 'Order withdrawn' } });
    const dialogs = screen.getAllByRole('dialog');
    fireEvent.click(within(dialogs[dialogs.length - 1]).getByRole('button', { name: 'Cancel it' }));
    await waitFor(() => expect(calls).toEqual([['close', { outcome: 'cancelled', note: 'Order withdrawn', version: 2 }]]));
  });

  it('saves an import source once and imports a file through it', async () => {
    const saved: unknown[] = [];
    // jsdom's FormData does not travel through the test fetch, so what the client appends is checked.
    const appended = vi.spyOn(FormData.prototype, 'append');
    const run: ImportRun = { id: 'run', source: 'Open purchase orders', file_name: 'open.csv', full_export: true,
      rows_total: 2, created: 2, unchanged: 0, revised: 0, conflicts: 0, problems: [{ row: 3, problem: 'It has no business reference.' }],
      problem_count: 1, missing: [], note: null, created_at: '2026-10-03T12:00:00', completed_at: '2026-10-03T12:00:01',
      imported_by: 'Dana', summary: '2 rows: 2 new, 0 unchanged, 0 new revisions, 0 changed without a new revision, 1 with problems.' };
    let sources: Array<Record<string, unknown>> = [];
    server.use(
      http.get('/expected-faxes', () => HttpResponse.json({ expected: [] })),
      http.get('/expected-faxes/counts', () => HttpResponse.json({ waiting: 0, overdue: 0, proposed: 0, missing: 0, conflicts: 0, matched: 0 })),
      http.get('/expected-faxes/mailboxes', () => HttpResponse.json({ mailboxes: [{ id: 'box-1', label: 'Purchasing' }] })),
      http.get('/expected-faxes/sources', () => HttpResponse.json({ sources })),
      http.get('/expected-faxes/imports', () => HttpResponse.json({ imports: [] })),
      http.post('/expected-faxes/sources', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        saved.push(body);
        sources = [{ id: 'src', version: 1, mailbox: 'Purchasing', due_hours: null, subject_template: null,
          subaddress_template: null, form_field: null, revision_field: null, ...body }];
        return HttpResponse.json(sources[0]);
      }),
      http.post('/expected-faxes/imports', () => HttpResponse.json(run)),
    );
    render(<ExpectedFaxes client={client()} canImport canOutage={false} />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Import' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Add an import source' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Open purchase orders' } });
    fireEvent.change(screen.getByLabelText(/Business reference/), { target: { value: 'PO Number' } });
    fireEvent.change(screen.getByLabelText('Who will send it'), { target: { value: 'Supplier' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(saved).toEqual([{ name: 'Open purchase orders', format: 'csv',
      mapping: { reference: 'PO Number', counterparty: 'Supplier' }, mailbox_id: null, due_hours: null,
      subject_template: null, subaddress_template: null, form_field: null, revision_field: null }]));
    await screen.findByText('Choose the export file');
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(['PO Number,Supplier\n1,Acme\n'], 'open.csv', { type: 'text/csv' })] } });
    fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    expect(await screen.findByText(/2 rows: 2 new/)).toBeTruthy();
    expect(screen.getByText('It has no business reference.')).toBeTruthy();
    const sent = Object.fromEntries(appended.mock.calls.map(([name, value]) => [name,
      typeof value === 'string' ? value : (value as File).name]));
    expect(sent).toEqual({ source: 'Open purchase orders', full_export: 'true', file: 'open.csv' });
    appended.mockRestore();
  });

  it('shows what was done during an outage and the three lists after it', async () => {
    const outage: Outage = { id: 'o1', code: 'QRT346', source: 'Open purchase orders', source_id: 'src',
      started_at: '2026-10-03T08:00:00', ended_at: '2026-10-03T12:00:00', note: null, declared_by: 'Dana',
      ended_by: 'Dana', open: false, version: 2, text: 'Down from 3 Oct 8:00 AM UTC to 3 Oct 12:00 PM UTC.',
      actions: [{ operation_id: '701', revision: null, reference: 'PO 701', action: 'Order faxed to Acme', channel: 'fax',
        outcome: 'done', fax_job_id: null, evidence_note: null, occurred_at: '2026-10-03T09:00:00', recorded_by: 'Dana' }],
      reconciliation: { id: 'r', created_at: '2026-10-03T12:05:00', created_by: 'Dana',
        summary: '1 already done (record them; do not submit them again), 1 new (submit them normally), 1 held for you to decide.',
        already_done: [{ operation_id: '701', revision: null, reference: 'PO 701', mailbox: 'Purchasing', code: null, reason: 'done',
          text: 'Already done during the outage; record it in the source system and do not submit it again.' }],
        new: [{ operation_id: '702', revision: null, reference: 'PO 702', mailbox: 'Purchasing', code: null, reason: 'new',
          text: 'Nothing was done during the outage; submit it normally.' }],
        unresolved: [{ operation_id: '999', revision: null, reference: 'Phone order', mailbox: null, code: null,
          reason: 'not_in_export', text: 'Recorded during the outage, but the new export does not list it; check it by hand.' }] } };
    server.use(
      http.get('/expected-faxes', () => HttpResponse.json({ expected: [] })),
      http.get('/expected-faxes/counts', () => HttpResponse.json({ waiting: 0, overdue: 0, proposed: 0, missing: 0, conflicts: 0, matched: 0 })),
      http.get('/expected-faxes/outages', () => HttpResponse.json({ outages: [outage] })),
      http.get('/expected-faxes/sources', () => HttpResponse.json({ sources: [] })),
      http.get('/expected-faxes/outages/:code', () => HttpResponse.json(outage)),
    );
    render(<ExpectedFaxes client={client()} canImport={false} canOutage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Outages' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }));
    expect(await screen.findByText(outage.reconciliation!.summary)).toBeTruthy();
    expect(screen.getByText('Already done: record it in the system, do not submit it again (1)')).toBeTruthy();
    expect(screen.getByText('Recorded during the outage, but the new export does not list it; check it by hand.')).toBeTruthy();
    expect(screen.getByText('PO 701: Order faxed to Acme')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sort the latest export again' })).toBeTruthy();
    expect(screen.queryByText('Record what was done')).toBeNull();  // the outage has ended
  });
});
