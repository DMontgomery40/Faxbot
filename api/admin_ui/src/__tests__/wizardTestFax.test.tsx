import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import WizardTestFax from '../components/WizardTestFax';
import { testPagePdf } from '../components/testPage';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const JOB = 'a'.repeat(32);

function job(status: string, extra: Record<string, unknown> = {}) {
  return { id: JOB, to_number: '+15555550123', status, delivery_state: status, backend: 'sip',
    created_at: '2026-10-03T12:00:00', updated_at: '2026-10-03T12:00:00', ...extra };
}

function sendHandlers(states: Array<Record<string, unknown>>) {
  const posts: Array<{ body: string; key: string | null }> = [];
  server.use(
    http.post('/fax', async ({ request }) => {
      posts.push({ body: await request.text().catch(() => ''), key: request.headers.get('Idempotency-Key') });
      return HttpResponse.json({ id: JOB, status: 'queued' }, { status: 202 });
    }),
    http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(states.length > 1 ? states.shift() : states[0])),
    http.get(`/routing/faxes/${JOB}/cost`, () => HttpResponse.json({ summary: 'Telnyx charged $0.005 for this call.' })),
  );
  return posts;
}

const send = (number: string) => {
  fireEvent.change(screen.getByLabelText('Fax number'), { target: { value: number } });
  fireEvent.click(screen.getByRole('button', { name: 'Send a test fax' }));
};

describe('Setup Wizard test fax', () => {
  it('sends one test page when asked, follows it live and shows the outcome and its cost', async () => {
    const posts = sendHandlers([job('in_progress'), job('success', { pages: 1 })]);
    const api = client();
    const sent = vi.spyOn(api, 'sendFax');
    render(<WizardTestFax client={api} sending="sip" receiving="" numbers={[]} pollMs={5} />);
    expect(screen.queryByTestId('test-fax-outcome')).toBeNull();
    send('+15555550123');
    expect((await screen.findByTestId('test-fax-outcome')).textContent).toBe('Sent: the receiving machine confirmed the test page.');
    expect(await screen.findByText('Telnyx charged $0.005 for this call.')).toBeTruthy();
    expect(posts).toHaveLength(1);
    expect(posts[0].key).toBeTruthy();
    // A one-page PDF a fax machine shows plainly: heading, time, provider and number.
    expect(sent).toHaveBeenCalledTimes(1);
    // A test proves the carrier path: it always asks for a real call, even to one of your own numbers.
    expect(sent.mock.calls[0][2]).toMatchObject({ byCall: true });
    const [to, file] = sent.mock.calls[0];
    expect(to).toBe('+15555550123');
    expect(file.name).toBe('faxbot-test-page.pdf');
    expect(file.type).toBe('application/pdf');
    const page = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(file);
    });
    expect(page.startsWith('%PDF-1.4')).toBe(true);
    expect(page).toContain('(Faxbot test page) Tj');
    expect(page).toContain('(Sent through Carrier trunk to +15555550123) Tj');
  });

  it('after a T.38 call with no fax data back, says Faxbot uses audio fax now and never sends again by itself', async () => {
    const posts = sendHandlers([job('failed', { error: 'The call connected but no fax data came back from the carrier.' })]);
    server.use(http.get('/admin/sip/status', () => HttpResponse.json({ configured: true, applied: true,
      asterisk_connected: true, registration: 'registered', reachability: 'reachable', registration_text: '',
      reachability_text: '', t38_off_reason: 'no_data_back', suggest_audio: false, message: 'The trunk is ready.' })));
    render(<WizardTestFax client={client()} sending="sip" receiving="" numbers={[]} pollMs={5} />);
    send('+15555550123');
    expect((await screen.findByTestId('test-fax-outcome')).textContent)
      .toBe('The call connected but no fax data came back from the carrier.');
    expect(await screen.findByText('Faxbot now uses audio fax for new calls; send another test when you are ready.')).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(posts).toHaveLength(1);
  });

  it('offers audio fax when Faxbot could not switch by itself', async () => {
    sendHandlers([job('failed', { error: 'The call connected but no fax data came back from the carrier.' })]);
    const writes: Array<Record<string, unknown>> = [];
    let applied = 0;
    server.use(
      http.get('/admin/sip/status', () => HttpResponse.json({ configured: true, applied: true, asterisk_connected: true,
        registration: 'registered', reachability: 'reachable', registration_text: '', reachability_text: '',
        suggest_audio: true, message: 'The trunk is ready.' })),
      http.get('/admin/settings', () => HttpResponse.json({ _meta: { active_revision_id: 'rev-1', desired_revision_id: 'rev-1',
        generation: 1, apply_state: 'applied', pending_fields: [] } })),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ok: true, changed: true, _meta: { active_revision_id: 'rev-2', desired_revision_id: 'rev-2',
          generation: 2, apply_state: 'applied', restart_recommended: false } });
      }),
      http.post('/admin/sip/apply', () => { applied += 1; return HttpResponse.json({ ok: true, engine: 'restarting', message: 'Asterisk is restarting to use the new settings.' }); }),
    );
    render(<WizardTestFax client={client()} sending="sip" receiving="" numbers={[]} pollMs={5} />);
    send('+15555550123');
    fireEvent.click(await screen.findByRole('button', { name: 'Use audio fax for new calls' }));
    expect(await screen.findByText('Faxbot now uses audio fax for new calls; send another test when you are ready.')).toBeTruthy();
    expect(writes).toEqual([{ expected_revision_id: 'rev-1', sip_t38_enabled: false }]);
    expect(applied).toBe(1);
  });

  it('names the cloud provider while it sends the test page', async () => {
    sendHandlers([job('in_progress', { backend: 'humblefax' }), job('in_progress', { backend: 'humblefax' }), job('success', { backend: 'humblefax' })]);
    render(<WizardTestFax client={client()} sending="humblefax" receiving="" numbers={[]} pollMs={20} />);
    send('+15555550123');
    expect(await screen.findByText('HumbleFax is sending the test page…')).toBeTruthy();
    expect(screen.queryByText('The call is in progress…')).toBeNull();
    expect(await screen.findByText('Sent: the receiving machine confirmed the test page.')).toBeTruthy();
  });

  it('builds a valid one-page PDF whose cross-reference table points at each object', () => {
    const pdf = testPagePdf({ sentAt: 'October 3, 2026 at 10:47:55 PM MDT', installation: 'County (Clinic) é',
      provider: 'HumbleFax', destination: '+17208565062' });
    expect(pdf.startsWith('%PDF-1.4\n') && pdf.endsWith('%%EOF\n')).toBe(true);
    expect(/^[\x09\x0a\x20-\x7e]*$/.test(pdf)).toBe(true);
    const start = Number(/startxref\n(\d+)\n/.exec(pdf)?.[1]);
    expect(pdf.slice(start, start + 4)).toBe('xref');
    const offsets = [...pdf.slice(start).matchAll(/^(\d{10}) 00000 n $/gm)].map((match) => Number(match[1]));
    expect(offsets).toHaveLength(6);
    offsets.forEach((offset, index) => expect(pdf.slice(offset).startsWith(`${index + 1} 0 obj`)).toBe(true));
    expect(pdf).toContain('(From County \\(Clinic\\) ?) Tj');
  });

  it('shows a refusal in one sentence and sends nothing else', async () => {
    server.use(http.post('/fax', () => HttpResponse.json(
      { detail: 'Enter the full fax number with its area code, or with its country code starting with +.' }, { status: 400 })));
    render(<WizardTestFax client={client()} sending="phaxio" receiving="" numbers={[]} pollMs={5} />);
    send('555');
    expect((await screen.findByTestId('test-fax-outcome')).textContent)
      .toBe('Enter the full fax number with its area code, or with its country code starting with +.');
  });

  it('waits for a fax sent to the trunk number and says when it arrives', async () => {
    const lists = [[{ id: 'old', status: 'received', backend: 'sip' }],
      [{ id: 'old', status: 'received', backend: 'sip' }],
      [{ id: 'new', fr: '+13035550100', status: 'received', backend: 'sip' }, { id: 'old', status: 'received', backend: 'sip' }]];
    server.use(http.get('/inbound', () => HttpResponse.json(lists.length > 1 ? lists.shift() : lists[0])));
    render(<WizardTestFax client={client()} sending="" receiving="sip" numbers={['+15555550100']} pollMs={5} />);
    expect(screen.getByTestId('receive-instructions').textContent)
      .toBe('Send a fax to +15555550100 from any fax machine or service; it will show up in your received faxes.');
    expect(screen.queryByText('Send a test fax (optional)')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Wait for a received fax' }));
    expect(await screen.findByText('Waiting for a fax…')).toBeTruthy();
    expect((await screen.findByTestId('test-receive-outcome')).textContent).toBe('A fax from +13035550100 arrived; it is in your received faxes.');
    await waitFor(() => expect(screen.queryByText('Waiting for a fax…')).toBeNull());
  });
});
