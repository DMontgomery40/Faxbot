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
const ON = 'On: your carrier bills this trunk by the minute, so when no fax machine answers within 50 seconds Faxbot '
  + 'hangs up and the call is billed as one minute instead of two.';
const OFF = 'Off: Faxbot waits the usual 60 seconds for a fax machine to answer, so a call no fax machine answers is '
  + 'billed as two minutes.';

function settings() {
  return { _meta: META, sip: { trunk: { preset: 'telnyx', auth: 'registration', host: '', port: 0, transport: '',
    username: 'faxbotuser', password: '***', password_set: true, outbound_proxy: '', caller_id: '+15555550100',
    dids: ['+15555550100'], t38_enabled: true, fax_preference_header: true, codecs: '',
    t38_error_correction: 'redundancy', t38_max_datagram: 400, fax_max_rate: 14400, fax_ecm: true,
    fax_compression: 'jbig', fax_fine: true, fax_answer_cap: true, sslfax_enabled: true, fax_lines: 2,
    sslfax_listener_port: 10443 } } };
}

describe('The answer cap on the trunk page', () => {
  it('shows why it is on, says what turning it off costs, and saves only the switch', async () => {
    const writes: Array<Record<string, unknown>> = [];
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings())),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get('/routing/stations/answer-cap', () => HttpResponse.json({ trunks: [
        { account: 'sip', label: 'Telnyx', on: true, applies: true, cap_seconds: 50, sentence: ON, on_sentence: ON,
          off_sentence: OFF }] })),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ok: true, changed: true, _meta: { active_revision_id: 'rev-2',
          desired_revision_id: 'rev-2', generation: 2, apply_state: 'applied', restart_recommended: false } });
      }),
    );
    render(<SipTrunkSettings client={new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' })} />);
    const section = await screen.findByTestId('fax-settings');
    fireEvent.click(within(section).getByRole('button', { name: 'Fax settings' }));
    const cap = within(section).getByTestId('answer-cap');
    const toggle = within(cap).getByRole('checkbox', { name: 'Hang up when no fax machine answers within 50 seconds (recommended)' });
    expect(toggle).toHaveProperty('checked', true);
    expect(await within(cap).findByText(ON)).toBeTruthy();
    fireEvent.click(toggle);
    expect(within(cap).getByText(OFF)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Save trunk settings' }));
    expect(await screen.findByText('Saved. Select Apply and connect to use it.')).toBeTruthy();
    expect(writes).toEqual([{ expected_revision_id: 'rev-1', sip_fax_answer_cap: false }]);
  });

  it('shows a general sentence when the trunk view cannot be read', async () => {
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings())),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get('/routing/stations/answer-cap', () => HttpResponse.json({ detail: 'unavailable' }, { status: 503 })),
    );
    render(<SipTrunkSettings client={new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' })} />);
    const section = await screen.findByTestId('fax-settings');
    fireEvent.click(within(section).getByRole('button', { name: 'Fax settings' }));
    expect(within(section).getByText(/a call no fax machine answers is billed as one minute instead of two/)).toBeTruthy();
  });
});
