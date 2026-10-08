// A fax whose call broke part way: only the pages the receiving machine did not confirm go again, and only on a click.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { CertaintyItem } from '../api/certaintyTypes';
import type { ContinuationOffer, ContinuationView } from '../api/continuationTypes';
import { FaxCertaintyItem } from '../components/work/SentCertainty';
import { SentContinuation, onTheirWay } from '../components/work/SentContinuation';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const BASIS = 'The call used error correction, and the receiving machine confirmed the first 7 pages whole.';
const WARNING = 'Page 8 may already have arrived, so the recipient may get it twice.';
const COST = 'About $0.91, against $1.40 for the whole fax.';

const offer = (overrides: Partial<ContinuationOffer> = {}): ContinuationOffer => ({
  available: true, reason: null, basis: BASIS, confirmed_pages: 7, open_item_id: null, may_send: true, first_page: 8,
  last_page: 20, pages: 13, total_pages: 20, pages_text: 'pages 8–20', action: 'Send pages 8–20', warning: WARNING,
  cost_text: COST, cost_account: 'phaxio', cost: null, ...overrides,
});

const view = (overrides: Partial<ContinuationView> = {}): ContinuationView => ({
  fax_id: 'fax-1', offer: offer(), continued_by: null, continues: null, ...overrides,
});

describe('remaining pages of a broken fax', () => {
  it('words one page and several pages', () => {
    expect(onTheirWay('pages 8–20')).toBe('Pages 8–20 are on their way as a new fax.');
    expect(onTheirWay('page 20')).toBe('Page 20 is on its way as a new fax.');
  });

  it('shows why these pages, their cost against the whole fax, and sends only on a click', async () => {
    const posted: unknown[] = [];
    server.use(
      http.get('/continuations/faxes/fax-1', () => HttpResponse.json(view())),
      http.post('/continuations/faxes/fax-1', async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json(view({ offer: null, continued_by: {
          fax_id: 'fax-2', first_page: 8, last_page: 20, pages_text: 'pages 8–20', sent_at: '2026-10-08T10:00:00',
          requested_by: 'Dana', from_item: false } }));
      }),
    );
    const opened: string[] = [];
    render(<SentContinuation client={client()} jobId="fax-1" onOpenFax={(faxId) => opened.push(faxId)} />);
    expect(await screen.findByText('Remaining pages')).toBeTruthy();
    for (const text of [BASIS, COST, WARNING]) expect(screen.getByText(text)).toBeTruthy();
    expect(posted).toEqual([]);
    fireEvent.change(screen.getByLabelText('Reason (optional)'), { target: { value: 'Rest of the referral' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send pages 8–20' }));
    await waitFor(() => expect(posted).toEqual([{ first_page: 8, reason: 'Rest of the referral' }]));
    expect(await screen.findByText('Pages 8–20 are on their way as a new fax.')).toBeTruthy();
    expect(screen.getByText('Pages 8–20 went as a new fax.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Send pages 8–20' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Open it' }));
    expect(opened).toEqual(['fax-2']);
  });

  it('says in one sentence why the pages cannot be told apart, and offers no button', async () => {
    const reason = 'Phaxio does not report how many pages it sent before a fax failed, so Faxbot cannot tell which pages arrived.';
    server.use(http.get('/continuations/faxes/fax-1', () => HttpResponse.json(view({ offer: offer({
      available: false, reason, first_page: undefined, action: undefined, warning: undefined, cost_text: undefined }) }))));
    render(<SentContinuation client={client()} jobId="fax-1" />);
    expect(await screen.findByText(reason)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('links a continuation back to the fax it continues', async () => {
    server.use(http.get('/continuations/faxes/fax-2', () => HttpResponse.json(view({ fax_id: 'fax-2', offer: null,
      continues: { fax_id: 'fax-1', first_page: 8, last_page: 20, pages_text: 'pages 8–20' } }))));
    const opened: string[] = [];
    render(<SentContinuation client={client()} jobId="fax-2" onOpenFax={(faxId) => opened.push(faxId)} />);
    expect(await screen.findByText('This fax carries pages 8–20 of an earlier fax whose call broke.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open it' }));
    expect(opened).toEqual(['fax-1']);
  });

  it('shows nothing for an ordinary fax, and leaves a waiting fax to its uncertain item', async () => {
    const { container, unmount } = render(<SentContinuation client={client()} jobId="fax-9" />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container.textContent).toBe('');
    unmount();
    server.use(http.get('/continuations/faxes/fax-1', () => HttpResponse.json(view({ offer: offer({ open_item_id: 'item-1' }) }))));
    const waiting = render(<SentContinuation client={client()} jobId="fax-1" />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(waiting.container.textContent).toBe('');
  });

  it('sits beside "send it again" on the uncertain item, and settles it with the remaining pages', async () => {
    const item: CertaintyItem = {
      id: 'item-1', fax_id: 'fax-1', reference: 'K7Q4MC', state: 'open', state_key: 'assigned',
      state_text: 'Assigned to Dana.', why: 'The call failed part way through, so some pages may have arrived.',
      category: 'partly_sent', to_number: '********0123', pages: 20, sent_at: '2026-10-07T09:00:00', mailbox: null,
      owner: { id: 'dana', name: 'Dana' }, owner_source_text: 'the person who sent it', due_at: null, due_hours: null,
      escalated_at: null, overdue: false, outcome: null, settled_by: null, settled_at: null, settled_reason: null,
      resend_fax_id: null, query_fax_id: null, is_mine: true, version: 3, actions: ['settle', 'send_query', 'continue'],
      suggestion: 'not_delivered', number: '+12025550123', moved_on: null, checks: [],
      continuation: { state: 'offered', first_page: 8, last_page: 20, pages: 13, total_pages: 20,
        pages_text: 'pages 8–20', action: 'Send pages 8–20', basis: BASIS, warning: WARNING, may_send: true,
        cost_text: COST },
    };
    const posted: unknown[] = [];
    server.use(
      http.get('/certainty/faxes/fax-1', () => HttpResponse.json({ items: [item], about: null })),
      http.post('/continuations/faxes/fax-1', async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json(view({ offer: null, item: { ...item, state: 'settled', state_key: 'settled',
          outcome: 'not_delivered', version: 4, actions: [], resend_fax_id: 'fax-2',
          state_text: 'Settled as not delivered by Dana. Pages 8–20 were sent as a new fax.',
          continuation: { state: 'sent', fax_id: 'fax-2', first_page: 8, last_page: 20, pages_text: 'pages 8–20' } } }));
      }),
      http.post('/certainty/items/item-1/settle', () => HttpResponse.json({}, { status: 500 })),
    );
    render(<FaxCertaintyItem client={client()} jobId="fax-1" />);
    expect(await screen.findByText('What happened to this fax?')).toBeTruthy();
    const again = screen.getByLabelText('Send it again now, as a new fax linked to this one') as HTMLInputElement;
    const rest = screen.getByLabelText('Send pages 8–20 only, as a new fax linked to this one') as HTMLInputElement;
    expect(screen.getByText(COST)).toBeTruthy();
    fireEvent.click(again);
    fireEvent.click(rest);
    expect(again.checked).toBe(false);
    expect(rest.checked).toBe(true);
    fireEvent.change(screen.getByLabelText('How do you know?'), { target: { value: 'The machine confirmed 7 pages' } });
    fireEvent.click(screen.getByRole('button', { name: 'Settle' }));
    await waitFor(() => expect(posted).toEqual([{ first_page: 8, reason: 'The machine confirmed 7 pages', version: 3 }]));
    expect(await screen.findByText('Settled. Pages 8–20 are on their way as a new fax.')).toBeTruthy();
    expect(screen.getByText('Settled as not delivered by Dana. Pages 8–20 were sent as a new fax.')).toBeTruthy();
  });
});
