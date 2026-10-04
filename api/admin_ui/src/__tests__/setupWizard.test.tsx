import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import SetupWizard from '../components/SetupWizard';
import { server } from '../test/server';
import { receipt, settingsFixture, withDirections } from '../test/settingsFixture';

type Json = Record<string, any>;

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const RAW_IDS = /\b(sip|phaxio|sinch|signalwire|documo|humblefax|freeswitch)\b/;
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

async function start(data: Json) {
  const writes = backend(data);
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
    await choose('Sending', 'SIP trunk (Asterisk)');
    await choose('Receiving', 'SIP trunk (Asterisk)');
    expect(screen.getByText('Sending: SIP trunk (Asterisk) · Receiving: SIP trunk (Asterisk)')).toBeTruthy();
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', backend: 'sip', inbound_enabled: true }]);
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
    expect(sectionHeadings()).toEqual(['For sending and receiving: SIP trunk (Asterisk)']);
    expect(await screen.findByTestId('sip-trunk-settings')).toBeTruthy();
    expect(screen.getByLabelText('Fax station ID')).toBeTruthy();
    // The fax engine connection is Faxbot's own business, out of the normal path.
    expect(screen.getByText('Advanced: fax engine connection')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(RAW_IDS);

    // Back shows the saved choice, not a draft.
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    await screen.findByText('Choose Providers', { selector: 'h6' });
    expect(screen.getByRole('combobox', { name: 'Sending' }).textContent).toBe('SIP trunk (Asterisk)');
    expect(writes).toHaveLength(1);
  });

  it('sends through a cloud provider and receives over the SIP trunk', async () => {
    const writes = await start(settingsFixture());
    await choose('Sending', 'HumbleFax');
    await choose('Receiving', 'SIP trunk (Asterisk)');
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', backend: 'humblefax', inbound_backend: 'sip', inbound_enabled: true }]);
    expect(sectionHeadings()).toEqual(['For sending: HumbleFax', 'For receiving: SIP trunk (Asterisk)']);
    // The trunk section, its Apply and the fax engine settings stay when sending uses another provider.
    expect(await screen.findByTestId('sip-trunk-settings')).toBeTruthy();
    expect(screen.getByText('Advanced: fax engine connection')).toBeTruthy();
    expect(screen.queryByLabelText('Fax station ID')).toBeNull();
    expect(screen.getByLabelText('Access Key')).toBeTruthy();
    // The inbound secret is Faxbot's own business: the step says whether received faxes reach Faxbot.
    expect((await screen.findByTestId('sip-handover')).textContent).toBe('Received faxes reach Faxbot: ready.');
    expect(screen.queryByLabelText(/inbound secret|Asterisk secret/i)).toBeNull();
    expect(document.body.textContent).not.toMatch(RAW_IDS);
  });

  it('sends over the SIP trunk and receives through a cloud provider', async () => {
    const writes = await start(settingsFixture());
    await choose('Sending', 'SIP trunk (Asterisk)');
    await choose('Receiving', 'Phaxio');
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', backend: 'sip', inbound_backend: 'phaxio', inbound_enabled: true }]);
    expect(sectionHeadings()).toEqual(['For sending: SIP trunk (Asterisk)', 'For receiving: Phaxio']);
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
    await choose('Receiving', 'SIP trunk (Asterisk)');
    next();
    expect(await screen.findAllByText('Choose a provider for sending as well; Faxbot needs one even when it mainly receives.'))
      .toHaveLength(2);
    expect(screen.getByText('Choose Providers', { selector: 'h6' })).toBeTruthy();
    expect(writes).toEqual([]);
    expect(document.body.textContent).not.toMatch(RAW_IDS);
  });

  it('offers only providers that can receive faxes for receiving', async () => {
    await start(settingsFixture());
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Receiving' }));
    const names = within(await screen.findByRole('listbox')).getAllByRole('option').map((option) => option.textContent);
    expect(names).toEqual(['No provider', 'Phaxio', 'Sinch', 'eFax', 'SIP trunk (Asterisk)']);
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
    await screen.findByText('Security Settings', { selector: 'h6' });
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
    expect(await screen.findByText('Save the SIP trunk settings first, or undo your changes there.')).toBeTruthy();
    expect(screen.getByText('Connect Providers', { selector: 'h6' })).toBeTruthy();
    expect(writes).toEqual([]);
  });
});
