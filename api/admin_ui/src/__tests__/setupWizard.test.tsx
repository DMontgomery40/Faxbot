import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import SetupWizard from '../components/SetupWizard';
import { server } from '../test/server';
import { receipt, settingsFixture, withDirections } from '../test/settingsFixture';

type Json = Record<string, any>;

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
// Provider ids on their own; a host name such as sip.telnyx.com is fine.
const RAW_IDS = /\b(sip|phaxio|sinch|signalwire|documo|humblefax|freeswitch)\b(?!\.)/;
const PRESETS = [{ id: 'telnyx', label: 'Telnyx', host: 'sip.telnyx.com', port: 5061, transport: 'tls',
  auth_modes: ['registration', 'ip'], codecs: ['ulaw', 'alaw'], needs_host: false, ip_dial_prefix: false, t38: '',
  notes: [], sources: [] }];

// A small stateful server: saving providers changes what /admin/settings reports
// and, like Faxbot, waits for a restart when the fax engine connection changes.
function backend(data: Json) {
  const writes: Json[] = [];
  let revision = 1;
  server.use(
    http.get('/admin/settings', () => HttpResponse.json(data)),
    http.get('/plugins', () => HttpResponse.json({ items: [] })),
    http.get('/admin/sip/presets', () => HttpResponse.json({ presets: PRESETS })),
    http.get('/admin/sip/status', () => HttpResponse.json({ configured: false, applied: false, asterisk_connected: false,
      registration: 'unknown', reachability: 'unknown', registration_text: '', reachability_text: '',
      handover_ready: data.inbound.enabled && data.hybrid.inbound_backend === 'sip' ? true : null,
      handover_text: data.inbound.enabled && data.hybrid.inbound_backend === 'sip' ? 'Received faxes reach Faxbot: ready.' : null,
      message: 'No SIP trunk is set up. Choose your carrier to start.' })),
    http.put('/admin/settings', async ({ request }) => {
      const body = await request.json() as Json;
      writes.push(body);
      if (body.expected_revision_id !== data._meta.desired_revision_id) {
        return HttpResponse.json({ detail: 'Configuration changed. Reload before applying edits.' }, { status: 409 });
      }
      const providers = ['backend', 'outbound_backend', 'inbound_backend', 'inbound_enabled'].some((name) => name in body);
      if (providers) {
        const sending = body.backend ?? data.backend.type;
        const enabled = body.inbound_enabled ?? data.inbound.enabled;
        const override = body.inbound_backend ?? data.hybrid.inbound_override;
        withDirections(data, sending, enabled ? (override || sending) : '');
      }
      if ('fax_station_id' in body) data.sip.station_id = body.fax_station_id;
      if ('sip_trunk_preset' in body) data.sip.trunk = { ...(data.sip.trunk ?? {}), preset: body.sip_trunk_preset };
      if ('sip_trunk_dids' in body) data.sip.trunk = { ...(data.sip.trunk ?? {}), dids: String(body.sip_trunk_dids).split(',') };
      revision += 1;
      const pending = providers && [data.hybrid.outbound_backend, data.hybrid.inbound_backend].includes('sip');
      data._meta = { ...data._meta, desired_revision_id: `rev-${revision}`, apply_state: pending ? 'pending_restart' : 'applied',
        pending_fields: pending ? ['fax_backend', 'provider_profiles'] : [] };
      if (!pending) data._meta.active_revision_id = `rev-${revision}`;
      return HttpResponse.json(receipt(`rev-${revision}`, pending));
    }),
  );
  return writes;
}

async function choose(name: 'Sending' | 'Receiving', option: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name }));
  fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: option }));
}

const next = () => fireEvent.click(screen.getByRole('button', { name: 'Next' }));

async function start(data: Json, presets: Json[] = PRESETS) {
  const writes = backend(data);
  server.use(http.get('/admin/sip/presets', () => HttpResponse.json({ presets })));
  render(<SetupWizard client={client()} />);
  await screen.findByText('Choose Providers', { selector: 'h6' });
  return writes;
}

function sectionHeadings() {
  return screen.queryAllByRole('heading', { level: 3 }).map((heading) => heading.textContent);
}

describe('Setup Wizard providers for sending and receiving', () => {
  it('connects one cloud provider for both directions in one section', async () => {
    const writes = await start(settingsFixture((data) => withDirections(data, 'phaxio', 'phaxio')));
    expect(screen.getByText('Sending: Phaxio · Receiving: Phaxio')).toBeTruthy();
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([]);
    expect(sectionHeadings()).toEqual(['For sending and receiving: Phaxio']);
    expect(screen.getByRole('button', { name: 'Show callback details' })).toBeTruthy();
    expect(screen.queryByTestId('sip-trunk-settings')).toBeNull();
    expect(document.body.textContent).not.toMatch(RAW_IDS);
  });

  it('saves the SIP trunk for both directions when moving on, then asks for a restart', async () => {
    const data = settingsFixture();
    const writes = await start(data);
    await choose('Sending', 'Telnyx');
    await choose('Receiving', 'Telnyx');
    expect(screen.getByText('Sending: Telnyx · Receiving: Telnyx')).toBeTruthy();
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', backend: 'sip', inbound_enabled: true, sip_trunk_preset: 'telnyx', sip_trunk_host: '', sip_trunk_transport: '', sip_trunk_dial_format: '', sip_trunk_auth: 'registration' }]);
    expect(screen.getByTestId('restart-notice').textContent).toContain('Restart Faxbot to start using these providers.');
    // Restart now: Faxbot goes away, answers again, and this step says the restart worked.
    const answers = [false, true];
    server.use(
      http.post('/admin/restart', () => {
        data._meta = { ...data._meta, active_revision_id: data._meta.desired_revision_id, apply_state: 'applied', pending_fields: [] };
        return HttpResponse.json({ ok: true });
      }),
      http.get('/health', () => (answers.shift() ?? true) ? HttpResponse.json({ status: 'ok' }) : HttpResponse.error()),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Restart now' }));
    expect(await screen.findByText('Faxbot restarted and is using the saved settings.', {}, { timeout: 5000 })).toBeTruthy();
    expect(screen.queryByTestId('restart-notice')).toBeNull();
    expect(screen.getByText('Connect Providers', { selector: 'h6' })).toBeTruthy();
    expect(sectionHeadings()).toEqual(['For sending and receiving: Telnyx']);
    expect(await screen.findByTestId('sip-trunk-settings')).toBeTruthy();
    expect(screen.getByLabelText('Fax station ID')).toBeTruthy();
    // The fax engine connection is Faxbot's own business, out of the normal path.
    expect(screen.getByText('Fax engine connection (advanced)')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(RAW_IDS);

    // Back shows the saved choice, not a draft.
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    await screen.findByText('Choose Providers', { selector: 'h6' });
    expect(screen.getByRole('combobox', { name: 'Sending' }).textContent).toBe('Telnyx');
    expect(writes).toHaveLength(1);
  });

  it('sends through a cloud provider and receives over the SIP trunk', async () => {
    const writes = await start(settingsFixture());
    await choose('Sending', 'HumbleFax');
    await choose('Receiving', 'Telnyx');
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', backend: 'humblefax', inbound_backend: 'sip', inbound_enabled: true, sip_trunk_preset: 'telnyx', sip_trunk_host: '', sip_trunk_transport: '', sip_trunk_dial_format: '', sip_trunk_auth: 'registration' }]);
    expect(sectionHeadings()).toEqual(['For sending: HumbleFax', 'For receiving: Telnyx']);
    // The trunk section, its Apply and the fax engine settings stay when sending uses another provider.
    expect(await screen.findByTestId('sip-trunk-settings')).toBeTruthy();
    expect(screen.getByText('Fax engine connection (advanced)')).toBeTruthy();
    expect(screen.queryByLabelText('Fax station ID')).toBeNull();
    expect(screen.getByLabelText('Access Key')).toBeTruthy();
    // The inbound secret is Faxbot's own business: the step says whether received faxes reach Faxbot.
    expect((await screen.findByTestId('sip-handover')).textContent).toBe('Received faxes reach Faxbot: ready.');
    expect(screen.queryByLabelText(/inbound secret|Asterisk secret/i)).toBeNull();
    expect(document.body.textContent).not.toMatch(RAW_IDS);
  });

  it('sends over the SIP trunk and receives through a cloud provider', async () => {
    const writes = await start(settingsFixture());
    await choose('Sending', 'Telnyx');
    await choose('Receiving', 'Phaxio');
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', backend: 'sip', inbound_backend: 'phaxio', inbound_enabled: true, sip_trunk_preset: 'telnyx', sip_trunk_host: '', sip_trunk_transport: '', sip_trunk_dial_format: '', sip_trunk_auth: 'registration' }]);
    expect(sectionHeadings()).toEqual(['For sending: Telnyx', 'For receiving: Phaxio']);
    expect(await screen.findByTestId('sip-trunk-settings')).toBeTruthy();
    expect(screen.getByLabelText('Fax station ID')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Show callback details' })).toBeTruthy();
    // The trunk only sends here, so nothing is said about received faxes.
    expect(screen.queryByTestId('sip-handover')).toBeNull();
    expect(document.body.textContent).not.toMatch(RAW_IDS);
  });

  it('works with no provider yet, and refuses receiving without a sending provider', async () => {
    const writes = await start(settingsFixture((data) => withDirections(data, '', '')));
    expect((await screen.findByTestId('no-provider')).textContent).toBe('No fax provider set up yet. Provider setup');
    next();
    expect(await screen.findByText('Choose a provider on the first step to connect it here.')).toBeTruthy();
    expect(writes).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    await screen.findByText('Choose Providers', { selector: 'h6' });
    await choose('Receiving', 'Telnyx');
    next();
    expect(await screen.findAllByText('Choose a provider for sending as well; Faxbot needs one even when it mainly receives.'))
      .toHaveLength(2);
    expect(screen.getByText('Choose Providers', { selector: 'h6' })).toBeTruthy();
    expect(writes).toEqual([]);
    expect(document.body.textContent).not.toMatch(RAW_IDS);
  });

  it('sets the trunk to a phone system in one step and shows its own guidance next', async () => {
    const avaya = { ...PRESETS[0], id: 'avaya-ipoffice', label: 'Avaya IP Office', host: '', kind: 'phone_system', auth_modes: ['ip'],
      notes: ['Faxbot connects to IP Office as a SIP line on your local network; IP Office keeps its own carrier lines.'],
      admin_steps: ['System, LAN1 (or LAN2), VoIP: tick SIP Trunks Enable.'] };
    const writes = await start(settingsFixture((data) => { data.numbers = { default_country: 'GB',
      example: { national: '0121 234 5678', international: '+44 121 234 5678' }, supported_countries: ['US', 'GB', 'AU'] }; }), [...PRESETS, avaya]);
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Sending' }));
    const names = within(await screen.findByRole('listbox')).getAllByRole('option').map((option) => option.textContent);
    expect(names).toEqual(expect.arrayContaining(['Your phone system', 'Avaya IP Office', 'Advanced', 'FreeSWITCH']));
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'Avaya IP Office' }));
    expect(screen.getByText(/^Sending: Avaya IP Office · Receiving: /)).toBeTruthy();
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes[0]).toMatchObject({ backend: 'sip', sip_trunk_preset: 'avaya-ipoffice', sip_trunk_auth: 'ip', sip_trunk_host: '' });
    expect(sectionHeadings()).toContain('For sending: Avaya IP Office');
    expect((await screen.findByTestId('sip-preset-chosen')).textContent).toContain('Phone system: Avaya IP Office.');
    expect(screen.getByText('Faxbot connects to IP Office as a SIP line on your local network; IP Office keeps its own carrier lines.')).toBeTruthy();
  });

  it('offers only providers that can receive faxes for receiving', async () => {
    await start(settingsFixture());
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Receiving' }));
    const names = within(await screen.findByRole('listbox')).getAllByRole('option').map((option) => option.textContent);
    expect(names).toEqual(['No provider', 'Fax services', 'eFax', 'Phaxio', 'Sinch Fax',
      'Your own fax line through a carrier', 'Telnyx']);
  });
});

describe('Setup Wizard and the SIP trunk form', () => {
  const sipBoth = () => settingsFixture((data) => {
    withDirections(data, 'sip', 'sip');
    data.sip.trunk = { preset: '', auth: 'registration', host: '', port: 0, transport: '', username: '', password: '',
      password_set: false, outbound_proxy: '', caller_id: '', dids: [], t38_enabled: true, fax_preference_header: false,
      codecs: '', external_address: '' };
  });

  it('shares one settings revision with the trunk form and keeps the station ID being typed', async () => {
    const data = sipBoth();
    data.sip.trunk.preset = 'telnyx';
    const writes = await start(data);
    next();
    await screen.findByTestId('sip-trunk-settings');
    fireEvent.change(await screen.findByLabelText('Fax station ID'), { target: { value: '+12025550111' } });
    fireEvent.change(await screen.findByLabelText('Add a number'), { target: { value: '+12025550123' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save trunk settings' }));
    await screen.findByText('Saved. Select Apply and connect to use it.');
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', sip_trunk_dids: '+12025550123' });
    await waitFor(() => expect((screen.getByLabelText('Fax station ID') as HTMLInputElement).value).toBe('+12025550111'));
    next();
    await screen.findByText('Security', { selector: 'h6' });
    // The step saves with the revision the trunk save produced.
    expect(writes[1]).toEqual({ expected_revision_id: 'rev-2', fax_station_id: '+12025550111' });
  });

  it('does not move on while the trunk form has unsaved changes', async () => {
    const data = sipBoth();
    data.sip.trunk.preset = 'telnyx';
    const writes = await start(data);
    next();
    fireEvent.change(await screen.findByLabelText('Add a number'), { target: { value: '+12025550123' } });
    next();
    expect(await screen.findByText('Save the fax line settings first, or undo your changes there.')).toBeTruthy();
    expect(screen.getByText('Connect Providers', { selector: 'h6' })).toBeTruthy();
    expect(writes).toEqual([]);
  });
});

describe('Setup Wizard time zone', () => {
  it('chooses the office time zone on the first step and saves it when moving on', async () => {
    const writes = await start(settingsFixture((data) => {
      withDirections(data, 'phaxio', 'phaxio');
      data.installation = { time_zone: '' };
    }));
    const zone = within(screen.getByTestId('time-zone'));
    expect(zone.getByText("Choose your office's time zone so the times in fax emails match your clocks.")).toBeTruthy();
    const input = zone.getByLabelText('Time zone');
    fireEvent.change(input, { target: { value: 'America/Den' } });
    fireEvent.click(await screen.findByRole('option', { name: 'America/Denver' }));
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', time_zone: 'America/Denver' }]);
  });

  it('shows the server sentence when it refuses a zone', async () => {
    await start(settingsFixture((data) => {
      withDirections(data, 'phaxio', 'phaxio');
      data.installation = { time_zone: 'America/Denver' };
    }));
    server.use(http.put('/admin/settings', () => HttpResponse.json(
      { detail: 'Choose a time zone from the list, such as America/Denver.' }, { status: 400 })));
    const input = within(screen.getByTestId('time-zone')).getByLabelText('Time zone') as HTMLInputElement;
    expect(input.value).toBe('America/Denver');
    fireEvent.change(input, { target: { value: 'Europe/Lon' } });
    fireEvent.click(await screen.findByRole('option', { name: 'Europe/London' }));
    next();
    expect(await screen.findByText(/^Choose a time zone from the list, such as America\/Denver\./)).toBeTruthy();
  });
});

describe('Setup Wizard owner-only settings', () => {
  it('locks owner-only settings for people who are not the owner, so their other changes still save', async () => {
    const data = settingsFixture((value) => {
      withDirections(value, 'phaxio', 'phaxio');
      value.owner_only = ['public_api_url', 'phaxio_verify_signature', 'enforce_public_https', 'audit_log_enabled', 'pdf_token_ttl_minutes'];
    });
    const writes = backend(data);
    render(<SetupWizard client={client()} isOwner={false} />);
    await screen.findByText('Choose Providers', { selector: 'h6' });
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    const address = screen.getByLabelText("This server's public address") as HTMLInputElement;
    expect(address.disabled).toBe(true);
    expect(screen.getByText(/Only the owner of this installation can change this\./)).toBeTruthy();
    expect((screen.getByLabelText('Check that status updates come from Phaxio') as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(screen.getAllByLabelText(/API Secret/)[0], { target: { value: 'synthetic-new-secret' } });
    next();
    await screen.findByText('Security', { selector: 'h6' });
    expect(writes).toHaveLength(1);
    expect(writes[0]).toEqual({ expected_revision_id: expect.any(String), phaxio_api_secret: 'synthetic-new-secret' });
    expect((screen.getByLabelText('Require HTTPS for document links') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText('Record events') as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByTestId('owner-only-note').textContent).toBe('Only the owner of this installation can change this.');
  });
});
