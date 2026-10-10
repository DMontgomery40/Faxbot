import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MailboxStationCheck, RecipientStationCheck, SentStationCheck } from '../components/StationCheck';
import { AdminAPIError } from '../api/client';

function fakeClient() {
  const requests: Array<{ method: string; path: string; body?: unknown }> = [];
  let mode = 'warn';
  let mailboxMode = 'warn';
  const stations: Array<{ station: string; source: string; seen_at: string | null; actor_name: string | null }> = [
    { station: '+1 720-555-0199', source: 'call', seen_at: '2026-10-09T15:00:00', actor_name: null }];
  const client = {
    async call<T>(request: { method: string; path: string; body?: unknown }): Promise<T> {
      requests.push(request);
      const body = (request.body ?? {}) as Record<string, any>;
      if (request.path.startsWith('/routing/stations/faxes/')) {
        return { sentences: ['The number answered as +1 720-555-0199, a fax machine Faxbot did not expect there, so Faxbot hung up before any page. Check the number with the recipient.'] } as T;
      }
      if (request.path === '/routing/stations/mailboxes') {
        return { mailboxes: [{ mailbox_id: 'm-billing', label: 'Billing', mode: mailboxMode, chosen: mailboxMode === 'refuse',
          actor_name: mailboxMode === 'refuse' ? 'Ada Admin' : null,
          sentence: mailboxMode === 'refuse'
            ? 'When a number answers as another fax machine on a fax from Billing, Faxbot hangs up before any page. Chosen by Ada Admin.'
            : "When a number answers as another fax machine on a fax from Billing, the fax goes on and Sent details say so. This is Faxbot's default." }] } as T;
      }
      if (request.path.startsWith('/routing/stations/mailboxes/')) {
        mailboxMode = body.mode;
        return { sentence: 'Saved. When a number answers as another fax machine on a fax from Billing, Faxbot hangs up before any page.' } as T;
      }
      if (request.path.startsWith('/routing/stations/')) {
        if (request.method === 'PUT') {
          if (body.mode) mode = body.mode;
          if (body.station) stations.push({ station: body.station, source: 'person', seen_at: null, actor_name: 'Ada Admin' });
          return { number: '+13035550150', mode, mode_source: 'recipient', stations,
            sentence: body.mode ? 'Saved. When this number answers as another fax machine, Faxbot hangs up before any page.'
              : 'Saved. Faxbot expects this number to answer as that station too.' } as T;
        }
        return { number: '+13035550150', mode, mode_source: 'default', stations } as T;
      }
      throw new AdminAPIError(404, 'Not Found', 'Not Found');
    },
  };
  return { client, requests };
}

describe('the station check', () => {
  it('lets a recipient refuse another station and adds an expected one', async () => {
    const fake = fakeClient();
    render(<RecipientStationCheck client={fake.client} number="+13035550150" canWrite />);
    const region = await screen.findByRole('region', { name: 'Station check' });
    expect(within(region).getByText('Also expected: +1 720-555-0199')).toBeTruthy();
    fireEvent.mouseDown(within(region).getByRole('combobox', { name: 'What Faxbot does' }));
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'Hang up before any page' }));
    expect(await screen.findByText('Saved. When this number answers as another fax machine, Faxbot hangs up before any page.')).toBeTruthy();
    fireEvent.change(within(region).getByLabelText('Another fax number its machine shows'), { target: { value: '+1 303 555 0177' } });
    fireEvent.click(within(region).getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(fake.requests.filter((item) => item.method === 'PUT').map((item) => item.body))
      .toEqual([{ mode: 'refuse' }, { station: '+1 303 555 0177' }]));
  });

  it('shows what each mailbox does where it is set, saves a choice, and only shows it without settings changes', async () => {
    const fake = fakeClient();
    const { unmount } = render(<MailboxStationCheck client={fake.client} canWrite mailboxes={[{ id: 'm-billing', label: 'Billing' }]} />);
    const region = screen.getByRole('region', { name: 'Station check for mailboxes' });
    expect((await within(region).findByTestId('station-mode-m-billing')).textContent).toContain("This is Faxbot's default.");
    fireEvent.mouseDown(within(region).getByRole('combobox', { name: 'Mailbox' }));
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'Billing' }));
    // Choosing the mailbox shows what it does now; you change it to hang up.
    expect(within(region).getByRole('combobox', { name: 'What Faxbot does' }).textContent).toBe('Send anyway and say so in Sent');
    fireEvent.mouseDown(within(region).getByRole('combobox', { name: 'What Faxbot does' }));
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'Hang up before any page' }));
    fireEvent.click(within(region).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/^Saved\. When a number answers as another fax machine on a fax from Billing/)).toBeTruthy();
    await waitFor(() => expect(within(region).getByTestId('station-mode-m-billing').textContent).toContain('Chosen by Ada Admin.'));
    unmount();
    render(<MailboxStationCheck client={fake.client} canWrite={false} mailboxes={[{ id: 'm-billing', label: 'Billing' }]} />);
    const readOnly = screen.getByRole('region', { name: 'Station check for mailboxes' });
    expect((await within(readOnly).findByTestId('station-mode-m-billing')).textContent).toContain('Chosen by Ada Admin.');
    expect(within(readOnly).queryByRole('button', { name: 'Save' })).toBeNull();
  });

  it('says in Sent details which station answered', async () => {
    render(<SentStationCheck client={fakeClient().client} jobId="job-1" />);
    expect(await screen.findByTestId('sent-station-check')).toBeTruthy();
    expect(screen.getByText(/answered as \+1 720-555-0199/)).toBeTruthy();
  });
});
