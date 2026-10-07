import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import FaxMachinePanel from '../components/delivery/FaxMachinePanel';
import { server } from '../test/server';

const VIEW = {
  number: '+13035550150',
  sentence: 'From the last 3 calls with this number.',
  calls: [{
    when: '2026-10-07T18:00:00Z', direction: 'out', mode: 'T38', status: 'SUCCESS', rate_first: 14400, rate_lowest: 9600,
    trainings: 2, failures_to_train: 1, t38_after_ms: 10600, t38_by: 'faxbot', iaf: null, subaddress: null,
    sentences: ["The far end's fax machine accepts up to 14,400 bit/s, with error correction, MMR compression, fine pages, on A4 and B4 paper of any length.",
      'It started at 14,400 bit/s and went down to 9,600, in 2 trainings, 1 of them failed.'],
  }],
  learned: { t38_now: true, max_rate: 9600, inbound_rate: null,
    sentences: ['The last 3 faxes to this number switched to fax over IP only when Faxbot asked, about ten seconds after the answer, so Faxbot now asks at once.'] },
  iaf: null,
};

function client() {
  return new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
}

describe('Recipients → Details, their fax machine', () => {
  it('says what the fax machine said and what Faxbot learned, and approves fast fax with a name', async () => {
    const approved: unknown[] = [];
    server.use(
      http.get('/fax-machines/numbers/:number', () => HttpResponse.json(approved.length
        ? { ...VIEW, iaf: 'endpoint' } : VIEW)),
      http.get('/fax-machines/iaf', () => HttpResponse.json({ servers: approved.length ? [{ id: 's1', number: VIEW.number,
        kind: 'endpoint', label: 'Head office SR140', added_by: 'Dana Admin', added_at: '2026-10-07T18:00:00Z', removed_at: null }] : [],
      partners: [] })),
      http.post('/fax-machines/iaf', async ({ request }) => {
        approved.push(await request.json());
        return HttpResponse.json({ ok: true, server: { id: 's1', number: VIEW.number, kind: 'endpoint', label: 'Head office SR140',
          added_by: 'Dana Admin', added_at: '2026-10-07T18:00:00Z', removed_at: null } });
      }),
    );
    render(<FaxMachinePanel client={client()} number="+13035550150" canWrite />);
    const panel = await screen.findByTestId('fax-machine');
    expect(within(panel).getByText(VIEW.calls[0].sentences[0])).toBeTruthy();
    expect(within(panel).getByText(VIEW.learned.sentences[0])).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText('Name'), { target: { value: 'Head office SR140' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Use fast fax' }));
    expect(await within(panel).findByText('Faxes to and from this number now go as fast fax.')).toBeTruthy();
    expect(approved).toEqual([{ number: '+13035550150', kind: 'endpoint', label: 'Head office SR140' }]);
    expect(await within(panel).findByText(/Head office SR140\./)).toBeTruthy();
  });

  it('offers no change without settings write', async () => {
    server.use(
      http.get('/fax-machines/numbers/:number', () => HttpResponse.json(VIEW)),
      http.get('/fax-machines/iaf', () => HttpResponse.json({ servers: [], partners: [] })),
    );
    render(<FaxMachinePanel client={client()} number="+13035550150" canWrite={false} />);
    const panel = await screen.findByTestId('fax-machine');
    expect(within(panel).queryByRole('button', { name: 'Use fast fax' })).toBeNull();
  });
});
