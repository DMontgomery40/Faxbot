// A fax held while the office ran on battery goes by itself when the power allows: Sent says so in one sentence.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList from '../components/JobsList';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+12025550123';
const JOB = 'f'.repeat(32);
const job = { id: JOB, to_number: NUMBER, status: 'SUCCESS', backend: 'sip', pages: 20, created_at: '2026-10-10T15:41:00',
  updated_at: '2026-10-10T16:02:00', delivery_state: 'succeeded', dispatch_mode: 'normal', delivery_version: 3 };

function showing(details: Record<string, unknown>) {
  server.use(
    http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
    http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json({ ...job, ...details })),
  );
}

describe('Sent details for a fax held while on battery', () => {
  it('says when it was held and when it went', async () => {
    showing({ power_hold: 'Held at 9:41 AM MDT while on battery; sent at 10:02 AM MDT when the power came back.' });
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText(NUMBER));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect((await within(dialog).findByTestId('job-power-hold')).textContent).toBe(
      'Held at 9:41 AM MDT while on battery; sent at 10:02 AM MDT when the power came back.');
  });

  it('shows nothing for a fax that was never held for power', async () => {
    showing({ power_hold: null });
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText(NUMBER));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    await within(dialog).findByText('Route');
    expect(within(dialog).queryByTestId('job-power-hold')).toBeNull();
  });
});
