import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import FaxServersPanel from '../components/FaxServersPanel';
import { AdminAPIError } from '../api/client';

// The trunk page: fax servers such as RightFax that send through Faxbot (fax_servers.py).
const server = { id: 'a'.repeat(32), name: 'RightFax', address: '192.168.68.50', username: 'faxserver-aaaaaaaa',
  countries: ['US'], per_hour: 60, per_day: 500, dial_prefix: '9', contact_email: 'fax-admin@example.com',
  contact_webhook: null,
  sentence: 'RightFax at 192.168.68.50 may send to United States, up to 60 faxes an hour and 500 a day. Faxbot tells '
    + 'fax-admin@example.com when one of its faxes is not delivered.' };
const failedCall = { id: 'c1', server: 'RightFax', to: '+13035550199', dialled: '93035550199', state: 'failed',
  state_text: 'Failed', sentence: 'Faxbot could not deliver it: The line was busy', received_at: '2026-10-13T15:00:00',
  notice: 'sent', notice_text: 'Notice emailed to fax-admin@example.com.' };

function caller(servers: unknown[], calls: unknown[] = [], failAdd = false) {
  const sent: Array<{ method: string; path: string; body?: unknown }> = [];
  const call = vi.fn(async (request: { method: string; path: string; body?: unknown }) => {
    sent.push(request);
    if (request.path === '/admin/sip/fax-servers' && request.method === 'GET') {
      return { servers, where_text: 'In the fax server\'s SIP settings (on RightFax, the SR140\'s Primary Gateway), '
        + 'send fax calls to 192.168.68.230, port 5060, UDP, with T.38 on.' } as never;
    }
    if (request.path === '/admin/sip/fax-servers/calls') return { calls } as never;
    if (request.path === '/admin/sip/fax-servers' && request.method === 'POST') {
      if (failAdd) throw new AdminAPIError(400, 'Bad Request', 'Faxbot takes faxes to send on only from a fax server on your private network (10.x, 172.16-31.x or 192.168.x).');
      return { ...server, password: 'Synthetic-Once-Only', realm: 'faxbot',
        password_text: 'Enter this user name and password on the fax server now; Faxbot shows the password only once and keeps only its digest.',
        apply_text: 'Press Apply on the trunk page so the fax engine loads the fax server; it can send after that.' } as never;
    }
    if (request.method === 'PUT') return { ...server, per_day: 200, sentence: 'RightFax may now send 200 a day.' } as never;
    if (request.path.endsWith('/remove')) return { detail: 'RightFax can no longer send through Faxbot.' } as never;
    return {} as never;
  });
  return { call, sent };
}

describe('fax servers that send through Faxbot', () => {
  it('enrols RightFax, shows its password once, and sends what was typed', async () => {
    const { call, sent } = caller([]);
    render(<FaxServersPanel call={call} />);
    await waitFor(() => expect(screen.getByTestId('fax-servers-panel')).toBeTruthy());
    fireEvent.click(screen.getByText('Fax servers that send through Faxbot (RightFax and others)'));
    expect(screen.getByText(/Primary Gateway/)).toBeTruthy();
    expect(screen.getByText('No faxes from fax servers yet.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Its address on your network'), { target: { value: '192.168.68.50' } });
    fireEvent.change(screen.getByLabelText('Countries it may send to'), { target: { value: 'us, ca' } });
    fireEvent.change(screen.getByLabelText('Faxes a day'), { target: { value: '200' } });
    fireEvent.change(screen.getByLabelText('Outside-line prefix'), { target: { value: '9' } });
    fireEvent.change(screen.getByLabelText('Email when a fax is not delivered'),
      { target: { value: 'fax-admin@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add fax server' }));
    expect(await screen.findByText('Password: Synthetic-Once-Only')).toBeTruthy();
    expect(screen.getByText('User name: faxserver-aaaaaaaa')).toBeTruthy();
    const add = sent.find((request) => request.method === 'POST')!;
    expect(add.body).toEqual({ name: 'RightFax', address: '192.168.68.50', countries: ['US', 'CA'], per_hour: null,
      per_day: 200, dial_prefix: '9', contact_email: 'fax-admin@example.com', contact_webhook: null });
  });

  it('says why a fax server was refused', async () => {
    const { call } = caller([], [], true);
    render(<FaxServersPanel call={call} />);
    await waitFor(() => expect(screen.getByTestId('fax-servers-panel')).toBeTruthy());
    fireEvent.click(screen.getByText('Fax servers that send through Faxbot (RightFax and others)'));
    fireEvent.change(screen.getByLabelText('Its address on your network'), { target: { value: '8.8.8.8' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add fax server' }));
    expect(await screen.findByText(/only from a fax server on your private network/)).toBeTruthy();
  });

  it('lists each fax and what became of it, changes limits and takes a server away', async () => {
    const { call, sent } = caller([server], [failedCall]);
    render(<FaxServersPanel call={call} />);
    expect(await screen.findByText(server.sentence)).toBeTruthy();
    fireEvent.click(screen.getByText('Fax servers that send through Faxbot (RightFax and others)'));
    expect(screen.getByText('Faxbot could not deliver it: The line was busy')).toBeTruthy();
    expect(screen.getByText('Failed')).toBeTruthy();
    expect(screen.getByText('Told')).toBeTruthy();
    expect(screen.queryByText('2026-10-13T15:00:00')).toBeNull(); // shown in local time, never as stored
    fireEvent.click(screen.getByRole('button', { name: 'Change' }));
    const days = screen.getAllByLabelText('Faxes a day');
    fireEvent.change(days[0], { target: { value: '200' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('RightFax may now send 200 a day.')).toBeTruthy();
    const put = sent.find((request) => request.method === 'PUT')!;
    expect(put.path).toBe(`/admin/sip/fax-servers/${server.id}`);
    expect(put.body).toEqual({ per_hour: 60, per_day: 200, countries: ['US'], contact_email: 'fax-admin@example.com' });
    fireEvent.click(screen.getByRole('button', { name: 'Take it away' }));
    expect(await screen.findByText('RightFax can no longer send through Faxbot.')).toBeTruthy();
  });

  it('says when the list cannot be read or may not be read', async () => {
    const call = vi.fn(async () => { throw new AdminAPIError(403, 'Forbidden'); });
    render(<FaxServersPanel call={call as never} />);
    expect(await screen.findByText('You do not have permission to do this.')).toBeTruthy();
  });
});
