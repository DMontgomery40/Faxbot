import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { AdminAPIError } from '../api/client';
import type { CourtDetails, CourtFax, CourtProfile } from '../api/courtTypes';
import { CourtProfilePanel, CourtSendFields, EMPTY_COURT, SentCourt, courtJson } from '../components/CourtProfile';

// The California court profile (court/): Delivery setup → Routing rules, Send a fax and Sent details.
const profile: CourtProfile = {
  attached: [], summary: 'Faxes under this profile go exactly as given, by a fax call to the number given.',
  rules: [{ rule: 'rule 2.304', url: 'https://www.courts.ca.gov/cms/rules/index/two/rule2_304', read_on: '2026-10-10',
    says: 'Direct fax filing only to a court whose local rule provides for it.' }],
  holidays: { source: { url: 'https://www.courts.ca.gov/holidays.htm', read_on: '2026-10-10', says: '' },
    years: [2026, 2027], days: [] },
  courts: [{ county: 'Modoc', status: 'accepts', note: 'Civil, probate and family law.', fax: '(530) 233-6500',
    cutoff: '17:00', source: 'https://www.modoc.courts.ca.gov/rules.pdf', source_date: null, read_on: '2026-10-10' },
  { county: 'Mono', status: 'stopped', note: 'No longer accepts fax filings from 15 March 2025.', fax: null,
    cutoff: null, source: 'https://www.mono.courts.ca.gov/fax-filing', source_date: null, read_on: '2026-10-10' }],
  not_established: ['Fresno'], trend: 'California courts are leaving fax.',
  mailboxes: [{ id: 'm'.repeat(32), label: 'Litigation' }], routing_rules: [{ id: 'r-court', name: 'Court filings' }],
};

describe('the California court profile', () => {
  it('applies to a mailbox, lists the courts with sources and extends a deadline', async () => {
    const attached = { ...profile, changed: true, attached: [{ kind: 'mailbox' as const, target: 'm'.repeat(32),
      name: 'Litigation', sentence: 'Faxes from the Litigation mailbox go under the California court profile.',
      attached_at: null, by: 'Ada' }] };
    const client = {
      courtProfile: vi.fn().mockResolvedValue(profile),
      setCourtAttachment: vi.fn().mockResolvedValue(attached),
      courtDeadline: vi.fn().mockResolvedValue({ deadline: '2026-11-25', extended: '2026-12-01',
        sentence: 'A deadline of Wednesday 25 November 2026 is extended by two court days to Tuesday 1 December 2026.' }),
    };
    render(<CourtProfilePanel client={client} canWrite />);
    expect(await screen.findByText('California courts are leaving fax.')).toBeTruthy();
    expect(screen.getByText('Takes fax filing')).toBeTruthy();
    expect(screen.getByText('Stopped')).toBeTruthy();
    expect(screen.getByText(/Not established either way: Fresno/)).toBeTruthy();
    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]);
    fireEvent.click(await screen.findByRole('option', { name: 'Litigation' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply the court profile' }));
    expect(await screen.findByText('Faxes from the Litigation mailbox go under the California court profile.')).toBeTruthy();
    expect(client.setCourtAttachment).toHaveBeenCalledWith('mailbox', 'm'.repeat(32), true);
    fireEvent.change(screen.getByTestId('court-deadline'), { target: { value: '2026-11-25' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add two court days' }));
    expect(await screen.findByText(/extended by two court days to Tuesday 1 December 2026/)).toBeTruthy();
  });

  it('says what went wrong when the profile cannot load, and offers no change without write access', async () => {
    const failing = { courtProfile: vi.fn().mockRejectedValue(new AdminAPIError(403, 'Forbidden')),
      setCourtAttachment: vi.fn(), courtDeadline: vi.fn() };
    render(<CourtProfilePanel client={failing} canWrite={false} />);
    expect(await screen.findByText('You do not have permission to do this.')).toBeTruthy();
  });

  it('collects the case details on Send a fax and never sends a card', () => {
    let latest: CourtDetails | null = null;
    function Harness() {
      const [value, setValue] = useState<CourtDetails | null>(null);
      latest = value;
      return <CourtSendFields value={value} onChange={setValue} />;
    }
    render(<Harness />);
    fireEvent.click(screen.getByLabelText('Filing with a California court, or serving a party, by fax'));
    fireEvent.change(screen.getByTestId('court-county'), { target: { value: ' Modoc ' } });
    expect(latest).not.toBeNull();
    const body = JSON.parse(courtJson({ ...(latest as unknown as CourtDetails),
      documents: [{ title: 'Motion', pages: 2 }, { title: '', pages: 1 }] }));
    expect(body.county).toBe('Modoc');
    expect(body.documents).toEqual([{ title: 'Motion', pages: 2 }]);
    expect(JSON.stringify(body)).not.toMatch(/card/);
    const served = JSON.parse(courtJson({ ...EMPTY_COURT, purpose: 'serve', agreement_confirmed: true }));
    expect(served.agreement_confirmed).toBe(true);
    expect(served.sender).toBeUndefined();
  });

  it('shows a court fax in Sent with its timing and the transmission record', async () => {
    const court: CourtFax = {
      job_id: 'j'.repeat(32), purpose: 'serve', purpose_text: 'Service on a party by fax (rule 2.306)',
      case: { county: 'Modoc', case_number: 'CV-2026-0042', title: 'Ranch v. Feed', documents: [], served_party: 'Counsel' },
      source: 'sender', sentences: ['Sent under the California court profile.'],
      timing: ['The whole document was sent at 5:30 p.m. California time, after 5 p.m.'], court_note: null,
      record_ready: true,
    };
    const client = { courtFax: vi.fn().mockResolvedValue({ court }),
      downloadCourtRecord: vi.fn().mockResolvedValue(new Blob(['%PDF'], { type: 'application/pdf' })) };
    const created = vi.fn(() => 'blob:record');
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() });
    render(<SentCourt client={client} jobId={'j'.repeat(32)} />);
    expect(await screen.findByText(/Service on a party by fax \(rule 2.306\): Ranch v. Feed, Modoc County/)).toBeTruthy();
    expect(screen.getByText(/after 5 p.m./)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Download the transmission record' }));
    await waitFor(() => expect(client.downloadCourtRecord).toHaveBeenCalledWith('j'.repeat(32)));
  });

  it('shows nothing in Sent for an ordinary fax', async () => {
    const client = { courtFax: vi.fn().mockResolvedValue({ court: null }), downloadCourtRecord: vi.fn() };
    const { container } = render(<SentCourt client={client} jobId={'k'.repeat(32)} />);
    await waitFor(() => expect(client.courtFax).toHaveBeenCalled());
    expect(container.textContent).toBe('');
  });
});
