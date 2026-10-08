import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { DirectPartner } from '../api/deliveryTypes';
import DirectPartners from '../components/delivery/DirectPartners';
import { server } from '../test/server';

// Fax calls inside an encrypted tunnel with an enrolled partner (peer fax calls, M1b), on Recipients > Partners.
const partner: DirectPartner = {
  id: 'a'.repeat(32), organization: 'Valley Hospital', fax_number: '+13035550160', endpoint: 'https://valley.example',
  state: 'verified', status: 'Verified; faxes to this number are delivered directly.', code_sent: false,
  code_expires_at: null, verified_at: '2026-10-07T01:00:00', expires_at: null, version: 3,
  receive_fax_images: true, partner_receives_fax_images: false, fax_images_text: null,
  receive_peer_calls: false, partner_peer_calls: true, peer_call_address: null,
  peer_calls_text: 'Their Faxbot takes fax calls inside an encrypted tunnel. Enter its address inside the tunnel to use it.',
};

describe('fax calls inside a tunnel with a partner', () => {
  it('saves the address, takes their calls, checks the tunnel and says the tunnel is yours to set up', async () => {
    const sent: unknown[] = [];
    server.use(
      http.post('*/direct/peers/:id/peer-calls', async ({ request }) => {
        const body = await request.json() as { accept: boolean; address: string | null };
        sent.push(body);
        return HttpResponse.json({ ...partner, receive_peer_calls: body.accept, peer_call_address: '10.20.0.2:5070',
          detail: 'Saved. Press Apply on the SIP trunk page so the fax engine loads it.', partner_told: true });
      }),
      http.post('*/direct/peers/:id/peer-calls/check', () => HttpResponse.json({
        applies: false, reason: 'no_tunnel', tunnel: null,
        sentence: 'No encrypted tunnel reaches Valley Hospital, so Faxbot will not place a fax call to it.',
        note: "Set up the WireGuard tunnel to this partner yourself, inside the fax engine's network." })),
    );
    const changed = vi.fn();
    render(<DirectPartners client={new AdminAPIClient({ kind: 'key', key: 'synthetic-key' })} partners={[partner]}
      canWrite onChanged={changed} />);
    const group = screen.getAllByRole('group', { name: 'Fax calls inside a tunnel with Valley Hospital' })[0];
    expect(within(group).getByText(partner.peer_calls_text as string)).toBeTruthy();
    expect(within(group).getByText(/Set up a WireGuard tunnel to this partner on the fax engine's network yourself/)).toBeTruthy();
    fireEvent.change(within(group).getByLabelText('Their address inside the tunnel'), { target: { value: ' 10.20.0.2 ' } });
    fireEvent.click(within(group).getByLabelText('Take their fax calls inside the tunnel'));
    expect(await screen.findByText('Saved. Press Apply on the SIP trunk page so the fax engine loads it.')).toBeTruthy();
    expect(sent).toEqual([{ accept: true, address: '10.20.0.2' }]);
    fireEvent.click(within(group).getByRole('button', { name: 'Check the tunnel' }));
    expect(await screen.findByText(/No encrypted tunnel reaches Valley Hospital/)).toBeTruthy();
  });

  it('shows nothing about tunnel calls for a partner that is not verified, and no controls without write access', () => {
    const pending = { ...partner, state: 'pending' as const };
    render(<DirectPartners client={new AdminAPIClient({ kind: 'key', key: 'synthetic-key' })}
      partners={[pending, { ...partner, id: 'b'.repeat(32), organization: 'County Clinic' }]} canWrite={false}
      onChanged={() => undefined} />);
    expect(screen.queryByRole('group', { name: 'Fax calls inside a tunnel with Valley Hospital' })).toBeNull();
    const clinic = screen.getAllByRole('group', { name: 'Fax calls inside a tunnel with County Clinic' })[0];
    expect(within(clinic).queryByLabelText('Take their fax calls inside the tunnel')).toBeNull();
    expect(within(clinic).getByText(partner.peer_calls_text as string)).toBeTruthy();
  });
});
