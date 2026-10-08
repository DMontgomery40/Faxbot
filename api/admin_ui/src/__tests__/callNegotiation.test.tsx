// What fax calls negotiated (measurement only): the sentence on each fax's details, and the trunk page's
// summary with how many calls it was measured on.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList from '../components/JobsList';
import NegotiationSummary, { ReceivedCallNegotiation, callsPerFax, secondsPerPage } from '../components/CallNegotiation';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
const JOB = 'a'.repeat(32);
const MEASURE_ONLY = 'For one number at a time, Faxbot starts slower or uses a more robust compression only after its own calls to that number fail the same way more than once; it never turns error correction off or lowers resolution.';
const LAST_PAGE = 'The last page went at 9600 bit/s and had standard resolution; compression and error correction are not '
  + 'reported by this engine; 1 page in a 44 s call.';

const negotiation = (sentence: string) => ({
  engine: 'builtin', rate_first: null, rate_lowest: null, rate_last_page: 9600, trainings: null, compression: null,
  resolution: null, resolution_last_page: 'standard', ecm: null, sslfax: null, transfer_seconds: null,
  session_seconds: null, pages: 1, call_seconds: 44, sentence,
});

const group = (fields: Record<string, unknown>) => ({
  compression: null, ecm: null, speed_scope: null, speed: null, calls: 1, sent: 1, received: 0, delivered: 1, pages: 1,
  success_percent: 100, seconds_per_page: null, attempts_per_delivered: null, coding_label: 'Not reported by this engine',
  speed_label: 'Not reported by this engine', ...fields,
});

describe('How a fax call went', () => {
  it('shows the server\'s sentence on a received fax, and nothing when no phone-line call carried it', async () => {
    server.use(http.get('/admin/sip/negotiation/received/fax-1', () => HttpResponse.json(negotiation(LAST_PAGE))));
    const { unmount } = render(<ReceivedCallNegotiation client={client()} faxId="fax-1" />);
    const line = await screen.findByTestId('received-call-negotiation');
    expect(line.textContent).toBe(`How the call went${LAST_PAGE}`);
    unmount();
    render(<ReceivedCallNegotiation client={client()} faxId="fax-2" />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId('received-call-negotiation')).toBeNull();
  });

  it('shows what a sent fax\'s call negotiated in its details', async () => {
    const sentence = 'The call used MMR compression with error correction at 14400 bit/s, fine resolution; 2 pages in 41 s.';
    const job = { id: JOB, to_number: '+15550100001', status: 'success', backend: 'sip', pages: 2,
      created_at: '2026-10-06T12:00:00', updated_at: '2026-10-06T12:01:00', delivery_state: 'success',
      dispatch_mode: 'normal', delivery_version: 3,
      fax_engine: { engine: 'hylafax', sslfax: false, sentence: null, negotiation: negotiation(sentence) } };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
      http.get(`/admin/fax-jobs/${JOB}/delivery`, () => HttpResponse.json({
        version: 3, state: 'success', dispatch_mode: 'normal', provider_id: 'sip', profile_id: 'profile',
        revision_id: 'revision', attempt: { id: 'att-1', phase: 'success', provider_sid: null,
          submitted_at: '2026-10-06T12:00:10', completed_at: '2026-10-06T12:00:50' },
        can_bind_provider_identity: false, bind_refusal_reason: 'This fax already has a final result.', events: [],
        events_truncated: false })),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+15550100001'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    const line = await within(dialog).findByTestId('job-call-negotiation');
    expect(line.textContent).toBe(`How the call went${sentence}`);
  });
});

describe('How fax calls went, on the trunk page', () => {
  it('says how many calls it was measured on, labels last-page speeds and says it changes nothing', async () => {
    const asked: string[] = [];
    server.use(http.get('/admin/sip/negotiation', ({ request }) => {
      const days = new URL(request.url).searchParams.get('days') ?? '';
      asked.push(days);
      return HttpResponse.json({
        days: Number(days), calls: 7, measured_calls: 6, note: MEASURE_ONLY,
        sentence: `Measured on 6 calls in the last ${days} days; the engine reported nothing for 1 more call.`,
        groups: [
          group({ compression: 'MMR', ecm: 'on', speed_scope: 'call', speed: 14400, calls: 4, delivered: 3,
            success_percent: 75, seconds_per_page: 22.5, attempts_per_delivered: 1.5,
            coding_label: 'MMR compression with error correction', speed_label: '14400 bit/s' }),
          group({ speed_scope: 'last_page', speed: 14400, seconds_per_page: 60, attempts_per_delivered: 1,
            speed_label: '14400 bit/s on the last page' }),
          group({ calls: 1, delivered: 0, success_percent: 0 }),
        ],
      });
    }));
    render(<NegotiationSummary client={client()} />);
    const summary = await screen.findByTestId('negotiation-summary');
    await waitFor(() => expect(summary.textContent).toContain('Measured on 6 calls in the last 30 days'));
    const rows = within(summary).getAllByRole('row').map((row) => row.textContent);
    expect(rows[1]).toBe('MMR compression with error correction14400 bit/s475%22.5 s1.5');
    expect(rows[2]).toBe('Not reported by this engine14400 bit/s on the last page1100%60 s1');
    expect(rows[3]).toBe('Not reported by this engineNot reported by this engine10%——');
    expect(summary.textContent).toContain(MEASURE_ONLY);
    fireEvent.click(within(summary).getByRole('button', { name: 'Last 7 days' }));
    await waitFor(() => expect(summary.textContent).toContain('Measured on 6 calls in the last 7 days'));
    expect(asked).toEqual(['30', '7']);
  });

  it('says when it cannot be read', async () => {
    server.use(http.get('/admin/sip/negotiation', () => HttpResponse.json({ detail: 'x' }, { status: 403 })));
    render(<NegotiationSummary client={client()} />);
    expect(await screen.findByText('You do not have access to call measurements.')).toBeTruthy();
  });

  it('formats seconds and calls without inventing a value', () => {
    expect(secondsPerPage(null)).toBe('—');
    expect(callsPerFax(null)).toBe('—');
    expect(secondsPerPage(22.5)).toBe('22.5 s');
    expect(callsPerFax(1.25)).toBe('1.25');
  });
});
