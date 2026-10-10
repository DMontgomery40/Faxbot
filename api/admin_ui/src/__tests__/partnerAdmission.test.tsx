import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AdminAPIError } from '../api/client';
import type { PartnerAdmission as View } from '../api/admissionTypes';
import PartnerAdmission, { SentAdmission } from '../components/delivery/PartnerAdmission';

// Recipients → Partners: line windows and preflight with partners that take part (direct/admission.py).
const view: View = {
  settings: { lines: null, reserve: 1, window_minutes: 10, urgent: 'go', deadline: 'go' }, partner_lines: 1,
  lines_sentence: 'Partners that take part share 1 line; 1 stays for other callers.',
  policy_sentence: 'Urgent faxes start without a window; a fax that would miss its send-by time starts without one.',
  legacy: { calls: 3, sentence: "3 of the 5 faxes received in the last 7 days came from callers that don't take part." },
  not_a_receipt: 'A line window is only permission to start a call, never a receipt or a delivery.',
  partners: [{ peer_id: 'p'.repeat(32), organization: 'County Clinic', take_part: false, refused_classes: [],
    refusal_reason: null, version: 0, advice: 'Nothing measured yet: no calls to County Clinic in the last 30 days.',
    grants: [{ direction: 'received', outcome: 'granted', earliest: '2026-10-10T19:00:00', latest: '2026-10-10T19:10:00',
      duration_class: 'short', created_at: '2026-10-10T18:59:00', job_id: 'j'.repeat(32) }],
    preflights: [{ direction: 'asked', document_class: 'general', purpose: 'general', mailbox: 'Referrals',
      accepts: 1, reason: null, expires_at: null, created_at: '2026-10-10T18:59:00', job_id: 'j'.repeat(32) }] }],
};

describe('line windows and preflight with partners', () => {
  it('shows the lines, the reserve, a partner window and answer, and saves taking part', async () => {
    const saved = { ...view, partners: [{ ...view.partners[0], take_part: true }] };
    const client = { partnerAdmission: vi.fn().mockResolvedValue(view),
      saveAdmissionSettings: vi.fn().mockResolvedValue(view), saveAdmissionPartner: vi.fn().mockResolvedValue(saved) };
    render(<PartnerAdmission client={client} canWrite />);
    expect(await screen.findByText(view.lines_sentence)).toBeTruthy();
    expect(screen.getByText(view.not_a_receipt)).toBeTruthy();
    expect(screen.getByText(view.partners[0].advice)).toBeTruthy();
    expect(screen.getByText(/You received a short window from/)).toBeTruthy();
    expect(screen.getByText(/They accept general faxes at Referrals/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Take part: ask before paying to send by phone, and wait for line windows'));
    await waitFor(() => expect(client.saveAdmissionPartner).toHaveBeenCalledWith('p'.repeat(32),
      { take_part: true, refused_classes: [], refusal_reason: null }));
    fireEvent.change(screen.getByTestId('admission-lines'), { target: { value: '2' } });
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[0]);
    await waitFor(() => expect(client.saveAdmissionSettings).toHaveBeenCalledWith(
      { lines: 2, reserve: 1, window_minutes: 10, urgent: 'go', deadline: 'go' }));
  });

  it('says so when it cannot load, and Sent shows that a yes is not a receipt', async () => {
    const failing = { partnerAdmission: vi.fn().mockRejectedValue(new AdminAPIError(403, 'Forbidden')),
      saveAdmissionSettings: vi.fn(), saveAdmissionPartner: vi.fn() };
    render(<PartnerAdmission client={failing} canWrite={false} />);
    expect(await screen.findByText('You do not have permission to do this.')).toBeTruthy();
    const sent = { admissionFax: vi.fn().mockResolvedValue({ waiting: false, sentences: [
      "County Clinic's Faxbot said it accepts general faxes at Referrals. That is not a receipt: it says nothing about "
      + 'whether this fax arrived.'] }) };
    render(<SentAdmission client={sent} jobId={'j'.repeat(32)} />);
    expect(await screen.findByText(/That is not a receipt/)).toBeTruthy();
  });
});
