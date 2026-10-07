// Recipients → Case packets: where each document stands for the recipient, what a person records,
// the full-packet repair, the reuse period and the checklist builder. Synthetic numbers only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import CasePackets from '../components/delivery/CasePackets';
import { server } from '../test/server';

const keyClient = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const TO = '+15550100001';

const held = {
  case_id: 'case-7', to: TO, accepts_references: true, reuse_days: 90, reuse_days_default: 90, reuse_days_set: false,
  recipient_version: 0, packets_in_flight: 0, documents: [
    { id: 'e1', title: 'Medical record', pages: 40, reference: 'abcdef123456', source: 'Valley EHR', version: 'final',
      purpose: 'Appeal', state: 'accepted', accepted: true, accepted_at: '2026-10-03T12:00:00', accepted_how: 'partner_receipt',
      accepted_by: null, accepted_note: null, expires_at: '2027-01-01T12:00:00', sent_at: '2026-10-03T11:00:00', kept: true, fax_id: 'f'.repeat(32) },
    { id: 'e2', title: 'Cover letter', pages: 1, reference: 'bbbbbb123456', source: '', version: '', purpose: 'Appeal',
      state: 'sent', accepted: false, accepted_at: null, sent_at: '2026-10-03T11:00:00', kept: true, fax_id: 'f'.repeat(32) },
    { id: 'e3', title: 'Old labs', pages: 2, reference: 'cccccc123456', source: '', version: '', purpose: 'Appeal',
      state: 'expired', accepted: false, accepted_at: '2026-01-03T12:00:00', accepted_how: 'person', sent_at: '2026-01-02T11:00:00', kept: true, fax_id: 'e'.repeat(32) },
    { id: 'e4', title: 'Referral', pages: 3, reference: 'dddddd123456', source: '', version: '', purpose: 'Appeal',
      state: 'invalidated', accepted: false, accepted_at: '2026-10-01T12:00:00', invalidated_at: '2026-10-05T12:00:00',
      invalidated_note: 'Not in their system', sent_at: '2026-10-01T11:00:00', kept: true, fax_id: 'e'.repeat(32) },
  ],
};

async function open(canWrite = true) {
  const posts: Array<{ path: string; body: unknown }> = [];
  server.use(
    http.get('/cases/:caseId/documents', () => HttpResponse.json(held)),
    http.post('/cases/:caseId/accept', async ({ request }) => {
      posts.push({ path: 'accept', body: await request.json() });
      return HttpResponse.json(held);
    }),
    http.post('/cases/:caseId/invalidate', async ({ request }) => {
      posts.push({ path: 'invalidate', body: await request.json() });
      return HttpResponse.json(held);
    }),
    http.post('/cases/:caseId/repair', async ({ request }) => {
      const body = await request.json() as { preview: boolean };
      posts.push({ path: 'repair', body });
      return HttpResponse.json({ case_id: 'case-7', to: TO, pages: 46, documents: [
        { title: 'Medical record', pages: 40, status: 'included' }, { title: 'Cover letter', pages: 1, status: 'included' }],
      missing: [{ title: 'Fax from 2025', removed_at: null }, { title: 'Old referral', removed_at: '2026-10-06T03:00:00' }],
      packets_in_flight: 1, reason: 'x', fax_id: body.preview ? null : 'a'.repeat(32) }, { status: 202 });
    }),
    http.patch('/case-recipients/:number', async ({ request }) => {
      posts.push({ path: 'reuse', body: await request.json() });
      return HttpResponse.json({ to: TO, reuse_days: 30, reuse_days_default: 90, reuse_days_set: true, version: 1 });
    }),
  );
  render(<CasePackets client={keyClient()} canSend canWrite={canWrite} />);
  fireEvent.change(screen.getByLabelText('Case reference'), { target: { value: 'case-7' } });
  fireEvent.change(screen.getByLabelText('Recipient fax number'), { target: { value: TO } });
  fireEvent.click(screen.getByRole('button', { name: 'Look up' }));
  return { table: await screen.findByTestId('case-documents'), posts };
}

describe('Case packet acknowledgements', () => {
  it('shows delivered apart from acknowledged, with how and until when, too old, and not found', async () => {
    const { table } = await open();
    const row = (title: string) => within(table).getByText(title).closest('tr') as HTMLElement;
    expect(within(row('Medical record')).getByText('Acknowledged')).toBeTruthy();
    expect(within(row('Medical record')).getByText('version final · from Valley EHR')).toBeTruthy();
    expect(within(row('Medical record')).getByText(/by the partner's signed receipt; trusted until/)).toBeTruthy();
    expect(within(row('Cover letter')).getByText('Delivered, not acknowledged')).toBeTruthy();
    expect(within(row('Old labs')).getByText('Acknowledgement too old')).toBeTruthy();
    expect(within(row('Old labs')).getByText(/more than 90 days ago; the next packet sends it in full\./)).toBeTruthy();
    expect(within(row('Referral')).getByText("Recipient couldn't find it")).toBeTruthy();
    expect(within(row('Referral')).getByText('They said: "Not in their system". The next packet sends it in full.')).toBeTruthy();
    expect(screen.getByText("This recipient's acknowledgements are trusted for 90 days (Faxbot's default).")).toBeTruthy();
    expect(within(table).queryByText(/abcdef|ffffffff|e1/)).toBeNull();
  });

  it('records what the recipient said only for the chosen documents, with a note', async () => {
    const { posts } = await open();
    const confirmed = screen.getByRole('button', { name: 'Recipient confirmed' }) as HTMLButtonElement;
    expect(confirmed.disabled).toBe(true);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Choose Cover letter' }));
    fireEvent.click(confirmed);
    const record = screen.getByRole('button', { name: 'Record' }) as HTMLButtonElement;
    expect(record.disabled).toBe(true);  // a note, or their fax, first
    fireEvent.change(screen.getByLabelText(/^Note/), { target: { value: 'Their intake desk, by phone.' } });
    fireEvent.click(record);
    await waitFor(() => expect(posts).toEqual([{ path: 'accept', body: { to: TO, documents: ['e2'], note: 'Their intake desk, by phone.' } }]));
    expect(await screen.findByText(/Recorded\. Later packets list these documents/)).toBeTruthy();

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Choose Referral' }));
    fireEvent.click(screen.getByRole('button', { name: "Recipient couldn't find it" }));
    expect(screen.getByText('Faxbot stops listing these documents and sends them in full in the next packet. Nothing is sent now.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Record' }));
    await waitFor(() => expect(posts[1]).toEqual({ path: 'invalidate', body: { to: TO, documents: ['e4'] } }));
  });

  it('repairs only after a preview and a reason, and says what it cannot include', async () => {
    const { posts } = await open();
    fireEvent.click(screen.getByRole('button', { name: 'Repair: send the full packet' }));
    const send = screen.getByRole('button', { name: 'Send the full packet' }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    const plan = await screen.findByTestId('case-repair-plan');
    expect(plan.textContent).toContain('46 pages will be sent.');
    expect(plan.textContent).toContain("Not included: 'Fax from 2025' was sent before Faxbot kept original documents. Add it to the case again to include it.");
    expect(plan.textContent).toMatch(/Not included: 'Old referral' is no longer kept; your retention setting removed it on .*2026\. Add it to the case again to include it\./);
    expect(plan.textContent).toContain('An earlier packet for this case has not finished sending.');
    expect(send.disabled).toBe(true);  // still no reason
    fireEvent.change(screen.getByLabelText('Why the recipient needs every document again'), { target: { value: 'They lost the file.' } });
    fireEvent.click(send);
    expect(await screen.findByText('The full packet is queued as a new fax: 46 pages.')).toBeTruthy();
    expect(posts.map((post) => post.body)).toEqual([
      { to: TO, reason: '', preview: true }, { to: TO, reason: 'They lost the file.', preview: false }]);
  });

  it('sets how long acknowledgements are trusted, and shows it read-only to people who may not change it', async () => {
    const { posts } = await open();
    fireEvent.change(screen.getByLabelText('Trust acknowledgements for (days)'), { target: { value: '30' } });
    fireEvent.click(within(screen.getByTestId('case-reuse')).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(posts).toEqual([{ path: 'reuse', body: { reuse_days: 30, version: 0 } }]));
  });

  it('shows no change controls without permission to change the recipient', async () => {
    await open(false);
    expect(screen.queryByLabelText('Trust acknowledgements for (days)')).toBeNull();
    expect(screen.getByText("This recipient's acknowledgements are trusted for 90 days (Faxbot's default).")).toBeTruthy();
  });
});

describe('Checklist builder', () => {
  it('previews picks with reasons and missing items, and sends only what is checked', async () => {
    const builds: unknown[] = [];
    server.use(
      http.get('/cases/:caseId/originals', () => HttpResponse.json({ case_id: 'case-7', retention_days: 30, originals: [
        { id: 'o1', title: 'Discharge summary', pages: 3, reference: 'aaaa', document_type: 'Discharge summary',
          document_date: '2026-09-20T00:00:00', source: '', version: 'final', added_at: '2026-10-01T00:00:00', added_by: null },
        { id: 'o2', title: 'Lab results Mesa', pages: 2, reference: 'bbbb', document_type: 'Scanned pages',
          document_date: null, source: '', version: '', added_at: '2026-10-01T00:00:00', added_by: null }] })),
      http.get('/case-checklists', () => HttpResponse.json({ suggestions: true, example: { name: 'Example', items: [] }, checklists: [
        { id: 'c1', name: 'Discharge follow-up', version: 2, to: null, created_at: '2026-10-01T00:00:00', created_by: null, used: 1,
          items: [{ type: 'Discharge summary', required: true, within_days: 30, version: 'final' },
            { type: 'Lab results', required: true, within_days: null, version: null }] }] })),
      http.post('/cases/:caseId/checklist-packets', async ({ request }) => {
        const body = await request.json() as { preview: boolean; selection?: unknown[] };
        builds.push(body);
        return HttpResponse.json({ case_id: 'case-7', to: TO, as_of: '2026-10-07', purpose: '',
          checklist: { id: 'c1', name: 'Discharge follow-up', version: 2 },
          items: [{ type: 'Discharge summary', required: true, within_days: 30, version: 'final' },
            { type: 'Lab results', required: true, within_days: null, version: null }],
          selected: [{ item: 0, original_id: 'o1', title: 'Discharge summary', pages: 3, document_type: 'Discharge summary',
            document_date: '2026-09-20T00:00:00', version: 'final', source: '',
            reason: "Matches 'Discharge summary': version 'final', dated 20 September 2026, within 30 days of 7 October 2026." }],
          missing: [{ item: 1, type: 'Lab results', required: true, reason: "No 'Lab results' is in the case." }],
          required_not_selected: [{ item: 1, type: 'Lab results' }],
          suggestions: [{ item: 1, original_id: 'o2', title: 'Lab results Mesa', reason: "Its title or type mentions 'Lab results'. Check it before you add it." }],
          suggestions_enabled: true, packet: { pages: 3, pages_saved: 0, documents: [] },
          fax_id: body.preview ? null : 'a'.repeat(32) }, { status: 202 });
      }),
    );
    await open();
    fireEvent.click(screen.getByRole('button', { name: 'Open the checklist builder' }));
    const builder = await screen.findByTestId('case-checklist');
    expect(await within(builder).findByText('Lab results Mesa')).toBeTruthy();
    expect(within(builder).getByTestId('case-retention').textContent).toBe(
      'Faxbot removes a kept document 30 days after it was last added or sent, like sent fax files (Storage & retention).');
    fireEvent.click(within(builder).getByRole('button', { name: 'Build the packet' }));
    const preview = await screen.findByTestId('checklist-preview');
    expect(within(preview).getByText(/version 'final', dated 20 September 2026/)).toBeTruthy();
    expect(within(preview).getByText("Missing: Lab results. No 'Lab results' is in the case.")).toBeTruthy();
    const suggested = within(preview).getByRole('checkbox', { name: 'Send Lab results Mesa' }) as HTMLInputElement;
    expect(suggested.checked).toBe(false);
    const send = within(preview).getByRole('button', { name: 'Send the checked documents' }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);  // a required item is not covered
    fireEvent.click(within(preview).getByRole('checkbox', { name: /Send without Lab results; the recipient agreed/ }));
    fireEvent.click(send);
    await waitFor(() => expect(builds).toHaveLength(2));
    expect(builds[1]).toMatchObject({ preview: false, allow_missing: true, selection: [{ original_id: 'o1', item: 0 }], as_of: '2026-10-07' });
    expect(await screen.findByText('Queued to send: 3 pages.')).toBeTruthy();
  });
});
