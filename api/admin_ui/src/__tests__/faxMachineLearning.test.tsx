import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import FaxMachinePanel from '../components/delivery/FaxMachinePanel';
import { server } from '../test/server';

// Synthetic numbers only. Recipients → Details, "Their fax machine": calls joined across both fax engines, what Faxbot
// learned and changes for the number, and forgetting what failed.
const AUDIO = 'Fax over IP (T.38) to this number failed on 6 October, so Faxbot uses audio fax for it until 5 November.';
const COMPRESSION = 'Faxes to this number with JBIG compression failed 2 times in a row after its fax machine answered, so Faxbot uses MMR compression for it; the pages are the same, they take a little longer to send.';
const VIEW = {
  number: '+13035550150',
  sentence: 'From the last 2 calls with this number.',
  calls: [
    {
      when: '2026-10-07T18:00:00Z', direction: 'out', mode: 'audio', status: 'SUCCESS', rate_first: 9600, rate_lowest: 9600,
      trainings: 1, failures_to_train: 0, t38_after_ms: null, t38_by: null, iaf: null, subaddress: null,
      engine: 'hylafax', engine_label: "Faxbot's fast fax service", mode_label: 'Audio fax',
      outcome: 'Sent: 2 pages confirmed by the receiving machine.',
      sentences: ['The call used JBIG compression with error correction at 9600 bit/s, fine resolution; 2 pages in 18 s.'],
      changes: [AUDIO],
    },
    {
      when: '2026-10-06T18:00:00Z', direction: 'out', mode: 'T38', status: 'FAILED', rate_first: 14400, rate_lowest: 14400,
      trainings: 1, failures_to_train: 0, t38_after_ms: 10600, t38_by: 'faxbot', iaf: null, subaddress: null,
      engine: 'builtin', engine_label: null, mode_label: 'Fax over IP (T.38)',
      outcome: 'The other fax machine answered but the fax failed: The call dropped prematurely.',
      sentences: ['It started at 14,400 bit/s, in 1 training.', 'The call used MR compression, error correction.'],
      changes: [],
    },
  ],
  learned: {
    t38_now: false, max_rate: null, inbound_rate: null, audio: true, compression: 'mmr', ecm_on: false,
    sentences: [AUDIO, COMPRESSION],
    notes: ['Faxbot changes compression or error correction for a number only after at least 3 answered calls to it; this number has had 2 calls since Faxbot started learning.'],
    since: null,
  },
  memory: [{ direction: 'outbound', kind: 't38_failed', learned_at: '2026-10-06T18:01:00Z', expires_at: '2026-11-05T18:01:00Z',
    active: true, ended: null }],
  can_forget: true,
  iaf: null,
};

function client() {
  return new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
}

describe('Recipients → Details, their fax machine: what Faxbot learned', () => {
  it('shows each call from either engine, what Faxbot changes and why, and forgets what failed', async () => {
    const forgotten: string[] = [];
    server.use(
      http.get('/fax-machines/numbers/:number', () => HttpResponse.json(forgotten.length
        ? { ...VIEW, learned: { ...VIEW.learned, audio: false, sentences: [COMPRESSION] }, can_forget: false } : VIEW)),
      http.get('/fax-machines/iaf', () => HttpResponse.json({ servers: [], partners: [] })),
      http.post('/fax-machines/numbers/:number/forget', ({ params }) => {
        forgotten.push(String(params.number));
        return HttpResponse.json({ ok: true, forgotten: 1, sentence: 'Faxbot forgot what failed with +13035550150; its next calls use the usual settings.' });
      }),
    );
    render(<FaxMachinePanel client={client()} number="+13035550150" canWrite />);
    const panel = await screen.findByTestId('fax-machine');
    const calls = within(panel).getAllByTestId('fax-machine-call');
    expect(calls).toHaveLength(2);
    expect(within(calls[0]).getByText(/Sent · Audio fax · Faxbot's fast fax service/)).toBeTruthy();
    expect(within(calls[0]).getByText(VIEW.calls[0].outcome)).toBeTruthy();
    expect(within(calls[0]).getByText(AUDIO)).toBeTruthy();
    expect(within(calls[1]).getByText(/Sent · Fax over IP \(T\.38\)$/)).toBeTruthy();
    expect(within(calls[1]).getByText('The call used MR compression, error correction.')).toBeTruthy();
    expect(within(panel).getByText('What Faxbot changes for this number')).toBeTruthy();
    expect(within(panel).getByText(COMPRESSION)).toBeTruthy();
    expect(within(panel).getByText(VIEW.learned.notes[0])).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: 'Forget what failed' }));
    expect(await within(panel).findByText('Faxbot forgot what failed with this number; its next calls use the usual settings.')).toBeTruthy();
    expect(forgotten).toEqual(['+13035550150']);
    expect(within(panel).queryByRole('button', { name: 'Forget what failed' })).toBeNull();
  });

  it('offers no forgetting without settings write', async () => {
    server.use(
      http.get('/fax-machines/numbers/:number', () => HttpResponse.json(VIEW)),
      http.get('/fax-machines/iaf', () => HttpResponse.json({ servers: [], partners: [] })),
    );
    render(<FaxMachinePanel client={client()} number="+13035550150" canWrite={false} />);
    const panel = await screen.findByTestId('fax-machine');
    expect(within(panel).getByText(COMPRESSION)).toBeTruthy();
    expect(within(panel).queryByRole('button', { name: 'Forget what failed' })).toBeNull();
  });
});
