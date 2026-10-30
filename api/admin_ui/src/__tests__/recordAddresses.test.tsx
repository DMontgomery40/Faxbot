// Durable addresses for records (#54): every sent fax, received document, expected fax and
// recipient opens from its own address, list filters stay in that address, and rows open
// from a real link so the keyboard reaches them. Clicking a row itself does nothing.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { WorkItem } from '../api/types';
import JobsList from '../components/JobsList';
import Received from '../components/Received';
import ExpectedFaxes from '../components/expected/ExpectedFaxes';
import Destinations from '../components/delivery/Destinations';
import { focusPageHeading } from '../components/shell/focusHeading';
import { NAVIGATION, resolveHash } from '../navigation';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

// -- Sent --------------------------------------------------------------------------------------

const JOB = 'a'.repeat(32);
const job = {
  id: JOB, to_number: '+15550100001', status: 'failed', backend: 'phaxio', pages: 1,
  created_at: '2026-10-03T12:00:00', updated_at: '2026-10-03T12:01:00',
  delivery_state: 'failed', dispatch_mode: 'normal', delivery_version: 3,
};
function serveSent() {
  server.use(
    http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
    http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
    http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({
      version: 3, state: 'failed', dispatch_mode: 'normal', provider_id: 'phaxio', profile_id: 'p',
      revision_id: 'r', attempt: null, can_bind_provider_identity: false, bind_refusal_reason: null,
      events: [], events_truncated: false,
    })),
    http.get('/direct/deliveries', () => HttpResponse.json({ deliveries: [] })),
  );
}

describe('Sent record addresses', () => {
  it('gives the row a real link carrying the filter and fax, and opens only from it', async () => {
    serveSent();
    const onFaxChange = vi.fn();
    render(<JobsList client={client()} status="failed" faxId={null} onFaxChange={onFaxChange}
      recordHref={(id) => `#/faxes/sent?status=failed&fax=${id}`} />);
    const link = await screen.findByRole('link', { name: /\+15550100001/ });
    expect(link.getAttribute('href')).toBe(`#/faxes/sent?status=failed&fax=${JOB}`);
    // The row around the link is not itself a control: clicking another cell opens nothing.
    const row = link.closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByText('Failed'));
    expect(onFaxChange).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(link);
    expect(onFaxChange).toHaveBeenCalledWith(JOB);
  });

  it('opens the fax named in the address and reports closing back to the address', async () => {
    serveSent();
    const onFaxChange = vi.fn();
    render(<JobsList client={client()} faxId={JOB} onFaxChange={onFaxChange}
      recordHref={(id) => `#/faxes/sent?fax=${id}`} />);
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect(within(dialog).getByText(JOB)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(onFaxChange).toHaveBeenCalledWith(null);
  });

  it('still opens from the link on its own when no address is wired', async () => {
    serveSent();
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByRole('link', { name: /\+15550100001/ }));
    expect(await screen.findByRole('dialog', { name: 'Fax details' })).toBeTruthy();
  });
});

// -- Received ------------------------------------------------------------------------------------

const workItem = (overrides: Partial<WorkItem>): WorkItem => ({
  id: 'item', inbound_fax_id: 'fax', state: 'open', state_key: 'waiting', state_text: 'Waiting for an owner.',
  due_text: 'Acknowledge within 24 hours', due_at: null, due_hours: 24, due_source: 'installation',
  available_at: '2026-10-03T12:00:00', from_number: '+15550109999', to_number: '+15550100001', pages: 1,
  mailbox: 'Front Desk', owner: null, backup: null, assigned_at: null, acknowledged_by: null,
  acknowledged_at: null, escalated_at: null, done_at: null, done_by: null, done_note: null,
  duplicate_of: null, is_mine: false, overdue: false, version: 1, actions: [], ...overrides,
});

function serveReceived() {
  const openItem = workItem({ id: 'open-item', inbound_fax_id: 'fax-open', actions: ['document'] });
  server.use(
    http.get('/work', () => HttpResponse.json({ items: [openItem] })),
    http.get('/work/counts', () => HttpResponse.json({ open: 1, acknowledged: 0, done: 0, unassigned: 1, mine: 0, overdue: 0 })),
    http.get('/work/:id/history', () => HttpResponse.json({ events: [] })),
    http.get('/work/:id', ({ params }) => (params.id === 'open-item'
      ? HttpResponse.json(openItem)
      : HttpResponse.json({ detail: 'This operation is not permitted.' }, { status: 403 }))),
    http.get('/inbound', () => HttpResponse.json([
      { id: 'fax-open', fr: '+15550109999', to: '+15550100001', status: 'received', backend: 'phaxio', received_at: '2026-10-03T12:00:00' },
    ])),
  );
  return openItem;
}

describe('Received record addresses', () => {
  it('links the row to the document address and opens the one the address names', async () => {
    serveReceived();
    const onItemChange = vi.fn();
    const { rerender } = render(<Received client={client()} inboundEnabled permissions={new Set()}
      itemId={null} onItemChange={onItemChange} recordHref={(id) => `#/faxes/received?item=${id}`} />);
    const link = await screen.findByRole('link', { name: /9999/ });
    expect(link.getAttribute('href')).toBe('#/faxes/received?item=open-item');
    fireEvent.click(link);
    expect(onItemChange).toHaveBeenCalledWith('open-item');
    // A copied link (or Back) lands with the item in the address: its details open.
    rerender(<Received client={client()} inboundEnabled permissions={new Set()}
      itemId="open-item" onItemChange={onItemChange} recordHref={(id) => `#/faxes/received?item=${id}`} />);
    expect(await screen.findByText('History')).toBeTruthy();
  });

  it('shows a safe not-found state for a document this person may not see', async () => {
    serveReceived();
    render(<Received client={client()} inboundEnabled permissions={new Set()}
      itemId="someone-elses-item" onItemChange={vi.fn()} recordHref={(id) => `#/faxes/received?item=${id}`} />);
    expect(await screen.findByTestId('received-fax-not-found')).toBeTruthy();
  });
});

// -- Expected ------------------------------------------------------------------------------------

describe('Expected record addresses', () => {
  it('links the reference, keeps search in the address, and opens the named expected fax', async () => {
    const expectedFax = {
      id: 'b'.repeat(32), code: 'ACD347', reference: 'PO 483', kind: 'Signed acknowledgement', description: null,
      required_parts: [], required_revision: null, counterparty: 'Acme Supply', fax_numbers: [], direct_address: null,
      mailbox: 'Purchasing', mailbox_id: 'box-1', owner: null, state: 'open', state_key: 'waiting',
      state_text: 'Waiting; expected by 5 Oct 2:00 PM UTC.', due_at: '2026-10-05T14:00:00',
      due_text: 'Expected within 48 hours.', overdue: false, source: null, operation_id: null, revision: null,
      replaces: null, missing_from_export: false, conflict: false, matched_at: null, match: null, also_arrived: 0,
      proposals: [], keys: { subaddress: '483', email_subject: null, message_id: null, form_field: null },
      created_at: '2026-10-03T12:00:00', version: 2, actions: ['cancel'],
    };
    server.use(
      http.get('/expected-faxes', () => HttpResponse.json({ expected: [expectedFax] })),
      http.get('/expected-faxes/counts', () => HttpResponse.json({ waiting: 1, overdue: 0, proposed: 0, missing: 0, conflicts: 0, matched: 0 })),
      http.get('/expected-faxes/:code', () => HttpResponse.json(expectedFax)),
      http.get('/expected-faxes/:code/history', () => HttpResponse.json({ events: [] })),
    );
    const onCodeChange = vi.fn();
    const onSearchChange = vi.fn();
    const { rerender } = render(<ExpectedFaxes client={client()} canImport={false} canOutage={false}
      code={null} onCodeChange={onCodeChange} search="" onSearchChange={onSearchChange}
      recordHref={(code) => `#/faxes/expected?expected=${code}`} />);
    const link = await screen.findByRole('link', { name: /PO 483/ });
    expect(link.getAttribute('href')).toBe('#/faxes/expected?expected=ACD347');
    fireEvent.click(screen.getByText('Acme Supply'));
    expect(onCodeChange).not.toHaveBeenCalled();
    fireEvent.click(link);
    expect(onCodeChange).toHaveBeenCalledWith('ACD347');
    fireEvent.change(screen.getByLabelText('Search references'), { target: { value: 'PO' } });
    expect(onSearchChange).toHaveBeenCalledWith('PO');
    rerender(<ExpectedFaxes client={client()} canImport={false} canOutage={false}
      code="ACD347" onCodeChange={onCodeChange} recordHref={(code) => `#/faxes/expected?expected=${code}`} />);
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(await screen.findByText('History')).toBeTruthy();
  });
});

// -- Recipients ----------------------------------------------------------------------------------

describe('Recipient addresses', () => {
  it('links the number to its own address and opens details through the address', async () => {
    const onRecipientChange = vi.fn();
    const stub = {
      getDestination: vi.fn(async () => { throw new Error('This operation is not permitted.'); }),
      context: vi.fn(async () => { throw new Error('This operation is not permitted.'); }),
    } as unknown as AdminAPIClient;
    const destinations = [{
      number: '+12025550123', display_name: null, preferred_route: null, accepts_references: false,
      routes: [], delivered_costs: [], estimated_cost_30_days: [],
    }] as never[];
    render(<Destinations client={stub} destinations={destinations} canWrite={false} onChanged={vi.fn()}
      recipient={null} onRecipientChange={onRecipientChange}
      recordHref={(number) => `#/recipients/list?recipient=${encodeURIComponent(number)}`} />);
    const link = await screen.findByRole('link', { name: /\+12025550123/ });
    expect(link.getAttribute('href')).toBe('#/recipients/list?recipient=%2B12025550123');
    fireEvent.click(link);
    expect(onRecipientChange).toHaveBeenCalledWith('+12025550123');
    // The Details button stays for the keyboard too.
    expect(screen.getByRole('button', { name: 'Details for +12025550123' })).toBeTruthy();
  });
});

// -- Addresses resolve with record and filter together ---------------------------------------------

describe('record addresses resolve', () => {
  it('keeps the filter and the record when a copied link is opened', () => {
    const sent = resolveHash(NAVIGATION, '#/faxes/sent?status=failed&fax=abc123');
    expect(sent?.kind).toBe('page');
    expect(sent?.kind === 'page' && sent.address).toBe('#/faxes/sent?status=failed&fax=abc123');
    const received = resolveHash(NAVIGATION, '#/faxes/received?show=waiting&item=item-1');
    expect(received?.kind === 'page' && received.address).toBe('#/faxes/received?show=waiting&item=item-1');
    const expected = resolveHash(NAVIGATION, '#/faxes/expected?show=overdue&q=PO&expected=ACD347');
    expect(expected?.kind === 'page' && expected.address).toBe('#/faxes/expected?show=overdue&q=PO&expected=ACD347');
    const recipient = resolveHash(NAVIGATION, '#/recipients/list?recipient=%2B12025550123');
    expect(recipient?.kind === 'page' && recipient.address).toBe('#/recipients/list?recipient=%2B12025550123');
  });
});

// -- Focus on navigation --------------------------------------------------------------------------

describe('focusPageHeading', () => {
  it('moves focus to the page heading, making it focusable without entering the tab order', () => {
    const main = document.createElement('main');
    main.innerHTML = '<h1>Received</h1><p>Body</p>';
    document.body.appendChild(main);
    focusPageHeading(main);
    const heading = main.querySelector('h1') as HTMLElement;
    expect(document.activeElement).toBe(heading);
    expect(heading.tabIndex).toBe(-1);
    main.remove();
  });

  it('does nothing when there is no heading or no container', () => {
    const main = document.createElement('main');
    document.body.appendChild(main);
    focusPageHeading(main);
    expect(document.activeElement).toBe(document.body);
    focusPageHeading(null);
    main.remove();
  });
});
