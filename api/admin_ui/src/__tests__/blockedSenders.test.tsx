import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import BlockedSenders from '../components/BlockedSenders';
import MarkJunk from '../components/MarkJunk';
import { server } from '../test/server';

const ENTRY = {
  id: 'entry-1', number: '+13035550142', reason: 'Unsolicited offers', added_by: 'Dana Admin',
  added_at: '2026-10-07T18:00:00Z', expires_at: '2027-01-05T18:00:00Z', removed_at: null, removed_by: null,
  active: true, rejected_calls: 2, inbound_id: 'fax-1',
};
const VIEW = {
  sentence: 'Your fax engine turns away calls from 1 blocked number before answering, so they cost nothing.',
  synced: true, entries: [ENTRY],
  rejections: [{ id: '1791374400.1', number: '+13035550142', called: '+13035550100', rejected_at: '2026-10-07T19:00:00Z', entry_id: 'entry-1' }],
};

function client() {
  return new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
}

describe('Numbers → Blocked senders', () => {
  it('lists blocked numbers with why, who and until when, the calls turned away, and unblocks with one click', async () => {
    const removed: string[] = [];
    server.use(
      http.get('/screening', () => HttpResponse.json(removed.length ? { ...VIEW, entries: [{ ...ENTRY, active: false,
        removed_at: '2026-10-07T20:00:00Z', removed_by: 'Lee Admin' }] } : VIEW)),
      http.delete('/screening/senders/:entry', ({ params }) => {
        removed.push(String(params.entry));
        return HttpResponse.json({ ok: true, entry: { ...ENTRY, active: false } });
      }),
    );
    render(<BlockedSenders client={client()} canWrite />);
    const page = await screen.findByTestId('blocked-senders');
    expect(await within(page).findByText(VIEW.sentence)).toBeTruthy();
    const table = within(page).getByRole('table', { name: 'Blocked numbers' });
    expect(within(table).getByText('Unsolicited offers')).toBeTruthy();
    expect(within(table).getByText(/Dana Admin/)).toBeTruthy();
    expect(within(within(page).getByRole('table', { name: 'Calls turned away' })).getByText('+13035550100')).toBeTruthy();
    fireEvent.click(within(page).getByRole('button', { name: 'Unblock +13035550142' }));
    expect(await within(page).findByText('+13035550142 is no longer blocked.')).toBeTruthy();
    expect(removed).toEqual(['entry-1']);
    expect(await within(page).findByText(/unblocked by Lee Admin/)).toBeTruthy();
  });

  it('shows nothing to change without settings write', async () => {
    server.use(http.get('/screening', () => HttpResponse.json(VIEW)));
    render(<BlockedSenders client={client()} canWrite={false} />);
    const page = await screen.findByTestId('blocked-senders');
    await within(page).findByText(VIEW.sentence);
    expect(within(page).queryByRole('button', { name: /Unblock/ })).toBeNull();
    expect(within(page).queryByRole('button', { name: 'Block' })).toBeNull();
  });
});

describe('Mark sender as junk on a received fax', () => {
  it('asks why, blocks the fax\'s sender and says so; a refusal is shown in the dialog', async () => {
    const asked: unknown[] = [];
    let refuse = false;
    server.use(http.post('/screening/senders', async ({ request }) => {
      asked.push(await request.json());
      if (refuse) {
        return HttpResponse.json({ detail: "This fax's sender sent no number, so it can't be blocked. Faxbot never blocks callers who withhold their number." }, { status: 400 });
      }
      return HttpResponse.json({ ok: true, entry: ENTRY });
    }));
    const notices: string[] = [];
    render(<MarkJunk client={client()} inboundId="fax-1" from="•••0142" onDone={(text) => notices.push(text)} />);
    fireEvent.click(screen.getByRole('button', { name: 'Mark the sender •••0142 as junk' }));
    fireEvent.change(await screen.findByLabelText('Why it is junk'), { target: { value: 'Unsolicited offers' } });
    fireEvent.click(screen.getByRole('button', { name: 'Block sender' }));
    await screen.findByRole('button', { name: 'Mark the sender •••0142 as junk' });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(asked).toEqual([{ inbound_id: 'fax-1', reason: 'Unsolicited offers' }]);
    expect(notices).toEqual(['Calls from +13035550142 are now turned away before answering, for 90 days.']);
    refuse = true;
    fireEvent.click(screen.getByRole('button', { name: 'Mark the sender •••0142 as junk' }));
    fireEvent.change(await screen.findByLabelText('Why it is junk'), { target: { value: 'Junk' } });
    fireEvent.click(screen.getByRole('button', { name: 'Block sender' }));
    expect(await screen.findByText(/sent no number, so it can't be blocked/)).toBeTruthy();
  });
});
