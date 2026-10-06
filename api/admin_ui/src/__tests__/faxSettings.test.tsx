import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import SipTrunkSettings from '../components/SipTrunkSettings';
import { server } from '../test/server';

const PRESETS = [
  { id: 'telnyx', label: 'Telnyx', host: 'sip.telnyx.com', port: 5060, transport: 'udp',
    auth_modes: ['registration', 'ip'], codecs: ['ulaw', 'alaw'], needs_host: false, ip_dial_prefix: false,
    t38: '', notes: [], sources: [] },
];
const META = { active_revision_id: 'rev-1', desired_revision_id: 'rev-1', generation: 1, apply_state: 'applied',
  pending_fields: [] };

function settings(trunk: Record<string, unknown> = {}) {
  return { _meta: META, sip: { trunk: { preset: 'telnyx', auth: 'registration', host: '', port: 0, transport: '',
    username: 'faxbotuser', password: '***', password_set: true, outbound_proxy: '', caller_id: '+15555550100',
    dids: ['+15555550100'], t38_enabled: true, fax_preference_header: true, codecs: '',
    t38_error_correction: 'redundancy', t38_max_datagram: 400, fax_max_rate: 14400, fax_ecm: true,
    fax_compression: 'jbig', fax_fine: true, sslfax_enabled: true, fax_lines: 2, sslfax_listener_port: 10443,
    ...trunk } } };
}

describe('Fax settings on the trunk page', () => {
  it('is collapsed, starts at the recommended values and saves only what changed', async () => {
    const writes: Array<Record<string, unknown>> = [];
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings())),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ok: true, changed: true, _meta: { active_revision_id: 'rev-2',
          desired_revision_id: 'rev-2', generation: 2, apply_state: 'applied', restart_recommended: false } });
      }),
    );
    render(<SipTrunkSettings client={new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' })} />);
    const section = await screen.findByTestId('fax-settings');
    const summary = within(section).getByRole('button', { name: 'Fax settings' });
    expect(summary.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(summary);
    expect(within(section).getByRole('checkbox', { name: 'Error correction (recommended)' })).toHaveProperty('checked', true);
    expect(within(section).getByRole('checkbox', { name: 'Send pages faster when the other fax machine can (recommended)' }))
      .toHaveProperty('checked', true);
    // SSL Fax encrypts the pages but cannot check who answers: the hint says so, without overselling it.
    expect(within(section).getByText(/They travel encrypted, but Faxbot can't confirm who is at the other end, so this is as private as an ordinary fax call, not more\./))
      .toBeTruthy();
    expect(within(section).getByText('14,400 bits per second (recommended)')).toBeTruthy();
    fireEvent.click(within(section).getByRole('checkbox', { name: 'Error correction (recommended)' }));
    fireEvent.change(within(section).getByLabelText('Fax lines'), { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save trunk settings' }));
    expect(await screen.findByText('Saved. Select Apply and connect to use it.')).toBeTruthy();
    expect(writes).toEqual([{ expected_revision_id: 'rev-1', sip_fax_ecm: false, sip_fax_lines: 4 }]);
  });
});
