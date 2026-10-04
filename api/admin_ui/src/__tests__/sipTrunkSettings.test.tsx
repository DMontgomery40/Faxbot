import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import SipTrunkSettings, { connectedTime } from '../components/SipTrunkSettings';
import { server } from '../test/server';

const PRESETS = [
  { id: 'telnyx', label: 'Telnyx', host: 'sip.telnyx.com', port: 5060, transport: 'udp',
    auth_modes: ['registration', 'ip'], codecs: ['ulaw', 'alaw'], needs_host: false, ip_dial_prefix: false,
    t38: 'T.38 is turned on per number.', notes: ['The caller ID must be a number on your Telnyx account.'],
    sources: [{ url: 'https://sip.telnyx.com/', read_on: '2026-10-03' }] },
  { id: 'anveo', label: 'AnveoDirect', host: 'sbc.anveo.com', port: 5060, transport: 'udp', auth_modes: ['ip'],
    codecs: ['ulaw', 'alaw'], needs_host: false, ip_dial_prefix: false, t38: '', notes: [], sources: [] },
];

const META = { active_revision_id: 'rev-1', desired_revision_id: 'rev-1', generation: 1, apply_state: 'applied',
  pending_fields: [] };

function settings(trunk: Record<string, unknown> = {}) {
  return { _meta: META, sip: { trunk: { preset: 'telnyx', auth: 'registration', host: '', port: 0, transport: '',
    username: 'faxbotuser', password: '***', password_set: true, outbound_proxy: '', caller_id: '+15555550100',
    dids: ['+15555550100'], t38_enabled: true, fax_preference_header: false, codecs: '', ...trunk } } };
}

const CALLS = {
  items: [
    { id: 'c1', direction: 'outbound', job_id: 'j', attempt_id: 'a', trunk_preset: 'telnyx', did: '+15555550100',
      caller: '+15555550100', called: '+15555550123', started_at: '2026-10-03T12:00:00Z',
      answered_at: '2026-10-03T12:00:08Z', ended_at: '2026-10-03T12:01:13Z', disposition: 'answered',
      connected_seconds: 65, t38: 'yes', pages: 2, fax_status: 'SUCCESS', remote_station_id: null,
      error_cause: null, fax_preference: false, verdict: 'sent',
      summary: 'Sent: 2 pages confirmed by the receiving machine.' },
    { id: 'c2', direction: 'inbound', job_id: 'f', attempt_id: null, trunk_preset: 'telnyx', did: '+15555550100',
      caller: '+15555550199', called: '+15555550100', started_at: '2026-10-03T11:00:00Z', answered_at: null,
      ended_at: null, disposition: 'busy', connected_seconds: 0, t38: 'unknown', pages: null, fax_status: null,
      remote_station_id: null, error_cause: 'busy', fax_preference: false, verdict: null,
      summary: 'The number was busy.' },
    { id: 'c3', direction: 'inbound', job_id: null, attempt_id: null, trunk_preset: 'telnyx', did: '+15555550100',
      caller: '+13035550100', called: '+15555550100', started_at: '2026-10-03T10:00:00Z',
      answered_at: '2026-10-03T10:00:00Z', ended_at: '2026-10-03T10:00:14Z', disposition: 'answered',
      connected_seconds: 14, t38: 'yes', pages: 0, fax_status: 'FAILED', remote_station_id: null,
      error_cause: 'no_t38_data_back: The call dropped prematurely (cause 16)', fax_preference: false,
      verdict: 'no_t38_data_back',
      summary: 'A fax call from +13035550100 came in, but no fax data arrived from the carrier.' },
  ],
  next_cursor: 'older',
};

function client() {
  return new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
}

describe('SIP trunk settings', () => {
  it('saves only changed trunk fields, keeps the saved password, and adds a fax number', async () => {
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
    render(<SipTrunkSettings client={client()} />);
    expect(await screen.findByText('The caller ID must be a number on your Telnyx account.')).toBeTruthy();
    expect(screen.getByText('A password is saved.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Add a number'), { target: { value: '+15555550101' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Mark outgoing calls as fax when they start' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save trunk settings' }));
    expect(await screen.findByText('Saved. Apply the trunk to Asterisk to use it.')).toBeTruthy();
    expect(writes).toEqual([{ expected_revision_id: 'rev-1', sip_fax_preference_header: true,
      sip_trunk_dids: '+15555550100,+15555550101' }]);
  });

  it('sends UK numbers as typed for a UK installation and shows the numbers the server saved', async () => {
    const numbers = { default_country: 'GB', example: { national: '0121 234 5678', international: '+44 121 234 5678' },
      supported_countries: ['GB', 'US'] };
    let current: Record<string, unknown> = { ...settings({ caller_id: '', dids: [] }), numbers };
    const writes: Array<Record<string, unknown>> = [];
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(current)),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        current = { ...settings({ caller_id: '+441782684953', dids: ['+441782684953', '+441782684954'] }), numbers };
        return HttpResponse.json({ ok: true, changed: true, _meta: { active_revision_id: 'rev-2',
          desired_revision_id: 'rev-2', generation: 2, apply_state: 'applied', restart_recommended: false } });
      }),
    );
    render(<SipTrunkSettings client={client()} />);
    const callerId = await screen.findByLabelText(/Caller ID/);
    expect(screen.getByText('The numbers your carrier sends to this trunk, for example 0121 234 5678 or +44 121 234 5678.'))
      .toBeTruthy();
    expect(callerId.getAttribute('placeholder')).toBe('0121 234 5678');
    fireEvent.change(callerId, { target: { value: '01782 684953 ' } });
    for (const number of ['01782 684953', '01782 684954']) {
      fireEvent.change(screen.getByLabelText('Add a number'), { target: { value: number } });
      fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    }
    fireEvent.click(screen.getByRole('button', { name: 'Save trunk settings' }));
    expect(await screen.findByText('Saved. Apply the trunk to Asterisk to use it.')).toBeTruthy();
    expect(writes).toEqual([{ expected_revision_id: 'rev-1', sip_trunk_caller_id: '01782 684953',
      sip_trunk_dids: '01782 684953,01782 684954' }]);
    expect(await screen.findByText('+441782684954')).toBeTruthy();
    expect((screen.getByLabelText(/Caller ID/) as HTMLInputElement).value).toBe('+441782684953');
  });

  it('shows the server sentence when it cannot read a number', async () => {
    const detail = 'Enter the full fax number with its area code, or with its country code starting with +.';
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings())),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.put('/admin/settings', () => HttpResponse.json({ detail }, { status: 400 })),
    );
    render(<SipTrunkSettings client={client()} />);
    fireEvent.change(await screen.findByLabelText(/Caller ID/), { target: { value: '5550100' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save trunk settings' }));
    expect(await screen.findByText(detail)).toBeTruthy();
  });

  it('links one carrier documentation page instead of listing every source', async () => {
    const presets = [{ ...PRESETS[0], sources: [
      { url: 'https://developers.telnyx.com/docs/voice/sip-trunking/get-started', read_on: '2026-10-03' },
      { url: 'https://sip.telnyx.com/voice.json', read_on: '2026-10-03' },
      { url: 'https://support.telnyx.com/en/articles/1130672', read_on: '2026-10-03' }] }, PRESETS[1]];
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets })),
      http.get('/admin/settings', () => HttpResponse.json(settings())),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    const { container } = render(<SipTrunkSettings client={client()} />);
    const link = await screen.findByRole('link', { name: 'Telnyx documentation' });
    expect(link.getAttribute('href')).toBe('https://developers.telnyx.com/docs/voice/sip-trunking/get-started');
    expect(screen.getAllByRole('link').filter((item) => item.textContent?.includes('documentation'))).toHaveLength(1);
    expect(container.textContent).not.toContain('2026-10-03');
    expect(container.textContent).not.toContain('support.telnyx.com');
    expect(container.querySelector('a[href="https://sip.telnyx.com/voice.json"]')).toBeNull();
    expect(container.textContent).not.toMatch(/read \d/);
  });

  it('offers only the sign-in methods a carrier supports', async () => {
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings({ preset: 'anveo', auth: 'ip' }))),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    render(<SipTrunkSettings client={client()} />);
    expect(await screen.findByRole('radio', { name: 'Server IP address' })).toBeTruthy();
    expect(screen.queryByRole('radio', { name: 'Username and password' })).toBeNull();
    expect(screen.queryByLabelText('Password')).toBeNull();
  });

  it('applies to Asterisk and reports trunk status in plain sentences', async () => {
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings())),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.post('/admin/sip/apply', () => HttpResponse.json({ ok: true,
        message: 'Saved for Asterisk. Restart the Asterisk service to use these settings.' })),
      http.get('/admin/sip/status', () => HttpResponse.json({ configured: true, applied: true,
        asterisk_connected: true, registration: 'registered', registration_transport: 'tls',
        registration_text: "The carrier accepted Faxbot's registration over TLS.", reachability: 'reachable',
        reachability_text: "The carrier answered Faxbot's check in 38 ms.", round_trip_ms: 38,
        internet_address: '198.51.100.7', behind_router: true, port_numbers: 'changes',
        public_address_text: "Faxbot's internet address is 198.51.100.7; your network changes port numbers, so Telnyx has to follow Faxbot's packets, and the first test fax shows whether it does.",
        ports_text: 'No ports need to be opened or forwarded.',
        last_call_text: 'The call connected but no fax data came back from the carrier.',
        last_call_at: '2026-10-03T12:00:00Z', message: 'The trunk is ready.' })),
    );
    render(<SipTrunkSettings client={client()} />);
    await screen.findByText('A password is saved.');
    fireEvent.click(screen.getByRole('button', { name: 'Apply to Asterisk' }));
    expect(await screen.findByText('Saved for Asterisk. Restart the Asterisk service to use these settings.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Check trunk status' }));
    const status = await screen.findByTestId('sip-trunk-status');
    expect(within(status).getByText('The trunk is ready.')).toBeTruthy();
    expect(within(status).getByText("The carrier accepted Faxbot's registration over TLS.")).toBeTruthy();
    expect(within(status).getByText("The carrier answered Faxbot's check in 38 ms.")).toBeTruthy();
    expect(within(status).getByText(/^Faxbot's internet address is 198.51.100.7; your network changes port numbers/)).toBeTruthy();
    expect(within(status).getByText('No ports need to be opened or forwarded.')).toBeTruthy();
    expect(within(status).getByText(/^Last call, .*: The call connected but no fax data came back from the carrier\.$/)).toBeTruthy();
    expect(status.textContent).not.toMatch(/registered|reachable[^.]|no_t38|tls[^.]/);
  });

  it('refuses server IP sign-in behind a router in one sentence and names the transports plainly', async () => {
    const detail = 'Your Faxbot runs behind a router, so sign in with a username and password; server IP sign-in needs a public address.';
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [{ ...PRESETS[0], port: 5061, transport: 'tls' }] })),
      http.get('/admin/settings', () => HttpResponse.json(settings({ auth: 'ip' }))),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.post('/admin/sip/apply', () => HttpResponse.json({ detail }, { status: 400 })),
    );
    render(<SipTrunkSettings client={client()} />);
    await screen.findByRole('radio', { name: 'Server IP address' });
    fireEvent.click(screen.getByRole('button', { name: 'Apply to Asterisk' }));
    expect(await screen.findByText(detail)).toBeTruthy();
    fireEvent.mouseDown(screen.getByLabelText('Transport'));
    const options = await screen.findAllByRole('option');
    expect(options.map((option) => option.textContent)).toEqual(
      ['Default (Encrypted (recommended))', 'Encrypted (recommended)', 'TCP', 'UDP (older)']);
    expect(screen.getByLabelText(/Internet address/).getAttribute('placeholder')).toBe('Automatic');
  });

  it('shows the missing fields the server names when applying fails', async () => {
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings({ password_set: false }))),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.post('/admin/sip/apply', () => HttpResponse.json({ detail: 'Fill in the password before applying.' },
        { status: 400 })),
    );
    render(<SipTrunkSettings client={client()} />);
    await screen.findByText('Not saved yet.');
    fireEvent.click(screen.getByRole('button', { name: 'Apply to Asterisk' }));
    expect(await screen.findByText('Fill in the password before applying.')).toBeTruthy();
  });

  it('lists recent calls with results, minutes and pages, and pages back through older calls', async () => {
    const cursors: Array<string | null> = [];
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings())),
      http.get('/admin/sip/calls', ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor');
        cursors.push(cursor);
        return HttpResponse.json(cursor ? { items: [], next_cursor: null } : CALLS);
      }),
    );
    render(<SipTrunkSettings client={client()} />);
    const table = await screen.findByRole('table', { name: 'Recent calls' });
    const rows = within(table).getAllByRole('row');
    expect(within(rows[1]).getByText('+15555550123')).toBeTruthy();
    expect(within(rows[1]).getByText('Answered')).toBeTruthy();
    expect(within(rows[1]).getByText('1 min 5 s')).toBeTruthy();
    expect(within(rows[2]).getByText('Received')).toBeTruthy();
    expect(within(rows[2]).getByText('Busy')).toBeTruthy();
    expect(within(rows[2]).getByText('Not known')).toBeTruthy();
    expect(within(rows[1]).getByText('Sent: 2 pages confirmed by the receiving machine.')).toBeTruthy();
    expect(within(rows[3]).getByText('A fax call from +13035550100 came in, but no fax data arrived from the carrier.'))
      .toBeTruthy();
    expect(table.textContent).not.toContain('no_t38_data_back');
    fireEvent.click(screen.getByRole('button', { name: 'Show older calls' }));
    await waitFor(() => expect(cursors).toEqual([null, 'older']));
  });

  it('explains when call history is not permitted', async () => {
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
      http.get('/admin/settings', () => HttpResponse.json(settings())),
      http.get('/admin/sip/calls', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })),
    );
    render(<SipTrunkSettings client={client()} />);
    expect(await screen.findByText('You do not have access to call history.')).toBeTruthy();
  });

  it('formats connected time as minutes and seconds', () => {
    expect(connectedTime(null)).toBe('Not known');
    expect(connectedTime(42)).toBe('42 s');
    expect(connectedTime(120)).toBe('2 min');
    expect(connectedTime(125)).toBe('2 min 5 s');
  });
});
