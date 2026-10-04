// Providers, revised: the panel lists only the providers in use, by the names the
// operator knows; the Setup wizard names every choice, grouped; the trunk is named
// after its carrier everywhere; the trunk page puts the fax engine at the bottom.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import App from '../App';
import AdminAPIClient from '../api/client';
import ProvidersInUse from '../components/ProvidersInUse';
import Settings from '../components/Settings';
import { providerChoices } from '../components/common/ProviderDirections';
import { providerLabel, setProviderNames } from '../providerLabels';
import type { ConsoleContext } from '../api/types';
import { ALL_PERMISSIONS, backend, server } from '../test/server';
import { receipt, settingsFixture, withDirections } from '../test/settingsFixture';

const TRUNK = [
  { id: 'telnyx', label: 'Telnyx', kind: 'carrier' as const },
  { id: 'gamma', label: 'Gamma', kind: 'carrier' as const },
  { id: 'bt-one-voice', label: 'BT One Voice', kind: 'carrier' as const },
  { id: 'telstra-sip-connect', label: 'Telstra SIP Connect', kind: 'carrier' as const },
  { id: 'avaya-ipoffice', label: 'Avaya IP Office', kind: 'phone_system' as const },
  { id: 'avaya-aura', label: 'Avaya Aura', kind: 'phone_system' as const },
  { id: 'custom', label: 'Another carrier', kind: 'carrier' as const },
];

const titles = (groups: ReturnType<typeof providerChoices>) => groups.map((group) => [group.title, group.options.map((option) => option.label)]);

describe('Every provider choice, by name and grouped', () => {
  it('lists fax services, carriers (local ones first), phone systems and the advanced engine', () => {
    expect(titles(providerChoices(TRUNK, 'GB', false))).toEqual([
      ['Fax services', ['HumbleFax', 'eFax', 'Phaxio', 'Sinch Fax', 'SignalWire Fax', 'Documo']],
      ['Your own fax line through a carrier', ['Gamma — UK', 'BT One Voice — UK', 'Telnyx', 'Telstra SIP Connect — Australia', 'Another carrier']],
      ['Your phone system', ['Avaya IP Office', 'Avaya Aura']],
      ['Advanced', ['FreeSWITCH']],
    ]);
    expect(titles(providerChoices(TRUNK, 'AU', false))[1][1][0]).toBe('Telstra SIP Connect — Australia');
    // Receiving offers only what can receive.
    expect(titles(providerChoices(TRUNK, 'US', true))).toEqual([
      ['Fax services', ['eFax', 'Phaxio', 'Sinch Fax']],
      ['Your own fax line through a carrier', ['Telnyx', 'Gamma — UK', 'BT One Voice — UK', 'Telstra SIP Connect — Australia', 'Another carrier']],
      ['Your phone system', ['Avaya IP Office', 'Avaya Aura']],
    ]);
  });
});

describe('The trunk by its carrier name', () => {
  it('names the trunk after its carrier or phone system once the installation says which', () => {
    expect(providerLabel('sip')).toBe('Carrier trunk');
    setProviderNames({ sip: 'Avaya IP Office' });
    expect(providerLabel('sip')).toBe('Avaya IP Office');
    expect(providerLabel('humblefax')).toBe('HumbleFax');
  });
});

describe('Providers in the panel', () => {
  function useInstall() {
    const admin = backend.state.principals.get('p_admin')!;
    admin.permissions = ALL_PERMISSIONS.map(([permission]) => permission);
    backend.state.providerView = { plugins_enabled: false, install_enabled: false, active_outbound: 'humblefax',
      active_inbound: 'sip', extra_routes: [], trunk_preset: 'telnyx' };
    backend.state.providerNames = { sip: 'Telnyx' };
  }

  async function signIn() {
    render(<App />);
    await screen.findByRole('heading', { name: 'Sign in' });
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByText('Ada Admin');
  }

  it('lists In use, the providers in use by name, and Add or change a provider; other providers keep their addresses', async () => {
    useInstall();
    await signIn();
    const providers = await screen.findByRole('button', { name: 'Providers' });
    fireEvent.click(providers);
    const list = document.getElementById('nav-providers') as HTMLElement;
    const names = within(list).getAllByRole('link').map((link) => link.textContent);
    expect(names).toEqual(['In use', 'HumbleFax', 'Telnyx', 'Add or change a provider']);
    expect(within(list).getByRole('link', { name: 'Add or change a provider' }).getAttribute('href')).toBe('#/system/setup');
    // A provider not in use still opens at its address.
    window.location.hash = '#/providers/phaxio';
    await waitFor(() => expect(window.location.hash).toBe('#/providers/phaxio'));
    const crumbs = await screen.findByRole('navigation', { name: 'You are here' });
    await waitFor(() => expect(within(crumbs).getByText('Phaxio').getAttribute('aria-current')).toBe('page'));
    expect(within(list).queryByRole('link', { name: 'Phaxio' })).toBeNull();
  });
});

describe('In use', () => {
  it('lists what sends and what receives by name, each opening its page, and offers the Setup wizard', () => {
    const navigate = vi.fn();
    setProviderNames({ sip: 'Telnyx' });
    const context = { provider_view: { plugins_enabled: false, install_enabled: false, active_outbound: 'humblefax',
      active_inbound: 'sip', extra_routes: ['phaxio'], trunk_preset: 'telnyx' } } as unknown as ConsoleContext;
    render(<ProvidersInUse context={context} canChange onNavigate={navigate} />);
    const summary = screen.getByTestId('providers-in-use');
    expect(within(summary).getByText('Sending')).toBeTruthy();
    fireEvent.click(within(summary).getByRole('button', { name: 'Telnyx' }));
    fireEvent.click(within(summary).getByRole('button', { name: 'Phaxio' }));
    fireEvent.click(within(summary).getByRole('button', { name: 'Add or change a provider' }));
    expect(navigate.mock.calls.map(([destination]) => destination)).toEqual(['providers/trunk', 'providers/phaxio', 'system/setup']);
  });
});

describe('The trunk page', () => {
  it('opens with the carrier and keeps the fax engine connection in a collapsed box at the bottom', async () => {
    const data = settingsFixture((value) => {
      withDirections(value, 'humblefax', 'sip');
      value.sip.trunk = { preset: 'telnyx', dids: ['+17208565062'] };
    });
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(data)),
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [{ id: 'telnyx', label: 'Telnyx', host: 'sip.telnyx.com',
        port: 5061, transport: 'tls', auth_modes: ['registration', 'ip'], codecs: ['ulaw'], needs_host: false, ip_dial_prefix: false,
        t38: '', notes: ['The caller ID must be a number on your Telnyx account.'], sources: [] }] })),
      http.get('/admin/sip/status', () => HttpResponse.json({ configured: true, applied: true, asterisk_connected: true,
        registration: 'registered', reachability: 'reachable', registration_text: '', reachability_text: '', message: '' })),
    );
    setProviderNames({ sip: 'Telnyx' });
    render(<Settings client={new AdminAPIClient({ kind: 'key', key: 'synthetic-key' })} sections={['trunk']} title={providerLabel('sip')} />);
    expect(await screen.findByRole('heading', { name: 'Telnyx' })).toBeTruthy();
    expect(await screen.findByTestId('sip-preset-chosen')).toBeTruthy();
    expect(screen.getByText('The caller ID must be a number on your Telnyx account.')).toBeTruthy();
    const engine = screen.getByTestId('fax-engine-connection');
    const button = within(engine).getByRole('button', { name: 'Fax engine connection (advanced)' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Station ID')).toBeNull();
    // The engine box comes after the carrier's settings.
    const order = screen.getByTestId('sip-trunk-settings').compareDocumentPosition(engine);
    expect(order & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(button);
    expect(await within(engine).findByText('Fax engine address')).toBeTruthy();
    expect(within(engine).getByText('Fax engine secret for received faxes')).toBeTruthy();
  });
});

describe('Provider pages after 4b', () => {
  const keyClient = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

  function trunkServer(data: Record<string, any>, writes: Array<Record<string, unknown>>) {
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(data)),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ok: true, changed: true, _meta: { ...data._meta, desired_revision_id: 'rev-b' } });
      }),
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [{ id: 'telnyx', label: 'Telnyx', host: 'sip.telnyx.com',
        port: 5061, transport: 'tls', auth_modes: ['registration', 'ip'], codecs: ['ulaw'], needs_host: false, ip_dial_prefix: false,
        t38: '', notes: [], sources: [] }] })),
      http.get('/admin/sip/status', () => HttpResponse.json({ configured: true, applied: true, asterisk_connected: true,
        registration: 'registered', reachability: 'reachable', registration_text: '', reachability_text: '', message: '' })),
    );
  }

  it('checks the internet address every few minutes and keeps the Telnyx key on the Telnyx page', async () => {
    const writes: Array<Record<string, unknown>> = [];
    const data = settingsFixture((value) => {
      withDirections(value, 'humblefax', 'sip');
      value.sip.trunk = { preset: 'telnyx', dids: ['+17208565062'], external_address: '', public_address_check_minutes: 5 };
      value.sip.telnyx_api_key = '***';
      value.sip.telnyx_api_key_set = true;
      value._meta.env_managed = ['telnyx_api_key'];
    });
    trunkServer(data, writes);
    setProviderNames({ sip: 'Telnyx' });
    render(<Settings client={keyClient()} sections={['trunk']} title={providerLabel('sip')} canWrite />);
    const minutes = await screen.findByLabelText('Check the internet address every … minutes') as HTMLInputElement;
    expect(minutes.value).toBe('5');
    const key = screen.getByTestId('telnyx-key');
    expect(within(key).getByText('Key for reading Telnyx charges')).toBeTruthy();
    expect(within(key).getByDisplayValue('Set in .env')).toBeTruthy();
    fireEvent.change(minutes, { target: { value: '15' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save trunk settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', sip_public_address_check_minutes: 15 });
  });

  it('says what FreeSWITCH still needs, and titles each provider page in normal case', async () => {
    const data = settingsFixture((value) => {
      value.fs.problem = 'Enter the caller ID number your carrier gave you for FreeSWITCH.';
    });
    server.use(http.get('/admin/settings', () => HttpResponse.json(data)));
    const { unmount } = render(<Settings client={keyClient()} sections={['freeswitch']} title="FreeSWITCH (advanced)" />);
    expect((await screen.findByTestId('freeswitch-problem')).textContent)
      .toBe('Enter the caller ID number your carrier gave you for FreeSWITCH.');
    unmount();
    render(<Settings client={keyClient()} />);
    expect(await screen.findByRole('heading', { name: 'Phaxio' })).toBeTruthy();
    expect(screen.queryByText(/PHAXIO|Phaxio Configuration/)).toBeNull();
  });
});

describe('Names follow a saved provider change', () => {
  it('reads the console context again after a save changes providers, without a reload', async () => {
    const admin = backend.state.principals.get('p_admin')!;
    admin.permissions = ALL_PERMISSIONS.map(([permission]) => permission);
    backend.state.providerView = { plugins_enabled: false, install_enabled: false, active_outbound: 'humblefax',
      active_inbound: 'sip', extra_routes: [], trunk_preset: 'telstra-sip-connect' };
    backend.state.providerNames = { sip: 'Telstra SIP Connect' };
    const data = settingsFixture((value) => { withDirections(value, 'humblefax', 'sip'); });
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(data)),
      // Saving switches the trunk back to Telnyx, as the Setup wizard does.
      http.put('/admin/settings', () => {
        backend.state.providerView = { ...backend.state.providerView!, trunk_preset: 'telnyx' };
        backend.state.providerNames = { sip: 'Telnyx' };
        return HttpResponse.json(receipt('rev-b'));
      }),
    );
    render(<App />);
    await screen.findByRole('heading', { name: 'Sign in' });
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByText('Ada Admin');
    window.location.hash = '#/providers/sending';
    fireEvent.click(await screen.findByRole('button', { name: 'Providers' }));
    const list = document.getElementById('nav-providers') as HTMLElement;
    await waitFor(() => expect(within(list).getByRole('link', { name: 'Telstra SIP Connect' })).toBeTruthy());
    const receiving = await screen.findByLabelText('Receiving is on');
    fireEvent.click(receiving);
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(within(list).getByRole('link', { name: 'Telnyx' })).toBeTruthy());
    expect(within(list).queryByRole('link', { name: 'Telstra SIP Connect' })).toBeNull();
    // The page the lead saw: the In use list names the new carrier too, without a reload.
    expect(within(screen.getByTestId('providers-in-use')).getByText(/Telnyx/)).toBeTruthy();
    expect(screen.queryByText(/Telstra SIP Connect/)).toBeNull();
  });

  it('does not read the context again for a save that changes nothing it shows', async () => {
    const client = new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
    const heard: string[][] = [];
    const stop = client.onSettingsChanged((changed) => heard.push(changed));
    server.use(http.put('/admin/settings', () => HttpResponse.json(receipt('b'))));
    await client.updateSettings({ expected_revision_id: 'a', fax_header: 'County Clinic' });
    stop();
    await client.updateSettings({ expected_revision_id: 'b', backend: 'phaxio' });
    expect(heard).toEqual([['fax_header']]);
  });
});
