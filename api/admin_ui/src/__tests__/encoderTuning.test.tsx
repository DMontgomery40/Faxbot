// Smaller pages (lossless tuning): the per-number panel saves your choice with its warning, and the Sent detail
// shows what tuning sent. The server words every sentence; the console shows them as they come.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import RecipientCodingTuningPanel from '../components/delivery/RecipientCodingTuning';
import FaxSettings from '../components/FaxSettings';
import JobsList from '../components/JobsList';
import type { SipTrunkSettings } from '../api/sipTypes';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
const NUMBER = '+15550100002';
const WARNING = 'Turn this on only if this fax machine prints faxes from Faxbot correctly: a machine that cannot '
  + 'read the smallest page format may print garbled pages and still report them as received.';
const SSLFAX = 'The smallest page format is used only when pages go over the internet to this fax machine.';

function view(changes: Record<string, unknown> = {}) {
  return { number: NUMBER, tune: null, tune_jbig: false, setting: true, mr: true, jbig: 'sslfax', reasons: [],
    jbig_sentence: SSLFAX, warning: WARNING, ...changes };
}

describe('Smaller pages for one number', () => {
  it('shows what its calls use with the warning, and saves the switch', async () => {
    let saved: unknown = null;
    server.use(
      http.get('/routing/destinations/:number/coding-tuning', () => HttpResponse.json(view())),
      http.put('/routing/destinations/:number/coding-tuning', async ({ request }) => {
        saved = await request.json();
        return HttpResponse.json(view({ tune_jbig: true, jbig: 'always',
          jbig_sentence: 'Smallest page format: on for this number.',
          reasons: ['You turned on the smallest page format for this number.'] }));
      }),
    );
    render(<RecipientCodingTuningPanel client={client()} number={NUMBER} canWrite />);
    const panel = await screen.findByTestId('recipient-coding-tuning');
    expect(within(panel).getByTestId('coding-tuning-now').textContent).toBe(SSLFAX);
    expect(within(panel).getByTestId('coding-tuning-warning').textContent).toBe(WARNING);
    fireEvent.click(within(panel).getByLabelText('Also use the smallest page format for this number'));
    fireEvent.click(within(panel).getByText('Save for this number'));
    await within(panel).findByText('Saved for the next fax.');
    expect(saved).toEqual({ tune: null, tune_jbig: true });
    expect(within(panel).getByTestId('coding-tuning-now').textContent).toBe('Smallest page format: on for this number.');
  });

  it('shows why a number gets plain pages', async () => {
    const refused = 'This fax machine refused a page in the smallest page format, so Faxbot sends it plain pages.';
    server.use(http.get('/routing/destinations/:number/coding-tuning', () => HttpResponse.json(view({
      jbig: 'never', jbig_sentence: 'Smallest page format: off for this number.', reasons: [refused] }))));
    render(<RecipientCodingTuningPanel client={client()} number={NUMBER} canWrite={false} />);
    expect(await screen.findByText(refused)).toBeTruthy();
    expect(screen.queryByText('Save for this number')).toBeNull();
  });
});

describe('The smaller pages setting', () => {
  it('is on by default and saves off', () => {
    const changes: Array<[string, unknown]> = [];
    render(<FaxSettings form={{} as SipTrunkSettings} update={(key, value) => { changes.push([key, value]); }} />);
    fireEvent.click(screen.getByText('Fax settings'));
    const toggle = screen.getByLabelText('Make pages smaller without changing them (recommended)') as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    fireEvent.click(toggle);
    expect(changes).toEqual([['fax_tune_coding', false]]);
  });
});

describe('The Sent detail', () => {
  it('says what tuning sent', async () => {
    const JOB = 'c'.repeat(32);
    const sentence = 'Sent with JBIG, tuned: 81% shorter than MMR for these pages.';
    const tuned = 'Tuned JBIG sent 5,629 bytes where plain JBIG would have sent 29,003, the same pixels either way.';
    const job = { id: JOB, to_number: NUMBER, status: 'success', backend: 'sip', pages: 1,
      created_at: '2026-10-08T12:00:00', updated_at: '2026-10-08T12:01:00', delivery_state: 'success',
      dispatch_mode: 'normal', delivery_version: 3,
      coding: { requested: 'JBIG', negotiated: 'JBIG', measured: true, compared: 'MMR', pages: 1, bits: {},
        receiver_known: true, sentence, measured_sentence: null, tuned: ['JBIG'], tuning_refused: [],
        tuning_sentence: tuned } };
    server.use(
      http.get('/intake/sources/faxes/:id', () => HttpResponse.json({ detail: 'This fax did not come from a connector.' },
        { status: 404 })),
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
      http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({
        version: 3, state: 'success', dispatch_mode: 'normal', provider_id: 'sip', profile_id: 'profile',
        revision_id: 'revision', attempt: { id: 'att-1', phase: 'success', provider_sid: null,
          submitted_at: '2026-10-08T12:00:10', completed_at: '2026-10-08T12:00:50' },
        can_bind_provider_identity: false, bind_refusal_reason: 'This fax already has a final result.', events: [],
        events_truncated: false })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText(NUMBER));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    const line = await within(dialog).findByTestId('job-coding');
    await waitFor(() => expect(line.textContent).toBe(`Fax coding${sentence}${tuned}`));
  });
});
