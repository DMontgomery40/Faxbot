import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import AdminAPIClient from '../api/client';
import type { DirectPartner } from '../api/deliveryTypes';
import DirectPartners from '../components/delivery/DirectPartners';

// Recipients > Partners: which encoded pages a partner's Faxbot says it reads (payload_layouts, migration 0082).
const partner: DirectPartner = {
  id: 'a'.repeat(32), organization: 'Valley Hospital', fax_number: '+13035550160', endpoint: 'https://valley.example',
  state: 'verified', status: 'Verified; faxes to this number are delivered directly.', code_sent: false,
  code_expires_at: null, verified_at: '2026-10-07T01:00:00', expires_at: null, version: 3,
  receive_fax_images: true, partner_receives_fax_images: true, fax_images_text: null,
  payload_layouts: ['grid', 'run-coded', 'picture'],
  payload_layouts_text: 'Their Faxbot reads these encoded pages, and only these go to them: grid, run-coded, picture.',
};
const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

describe('encoded pages a partner reads', () => {
  it('says which encoded pages go to the partner, and that its Faxbot has not said when it has not', () => {
    const older = { ...partner, id: 'b'.repeat(32), organization: 'County Clinic', payload_layouts: null,
      payload_layouts_text: "Their Faxbot hasn't said which encoded pages it reads, so it gets only the ones every "
        + 'Faxbot reads (grid, run-coded, picture), or capacity pages too if you recorded that its decoder reads them.' };
    render(<DirectPartners client={client()} partners={[partner, older]} canWrite={false} onChanged={() => undefined} />);
    expect(screen.getAllByText(partner.payload_layouts_text as string).length).toBeGreaterThan(0);
    expect(screen.getAllByText(older.payload_layouts_text).length).toBeGreaterThan(0);
  });

  it('shows nothing for a removed partner or an older server', () => {
    const removed = { ...partner, state: 'revoked' as const, payload_layouts_text: null };
    const fromOlderServer = { ...partner, id: 'c'.repeat(32), organization: 'North Lab', payload_layouts: undefined,
      payload_layouts_text: undefined };
    render(<DirectPartners client={client()} partners={[removed, fromOlderServer]} canWrite
      onChanged={() => undefined} />);
    expect(screen.queryByText(/encoded pages/)).toBeNull();
  });
});
