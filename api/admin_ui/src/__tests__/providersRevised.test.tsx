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
import { settingsFixture, withDirections } from '../test/settingsFixture';

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
    expect(await within(engine).findByText('AMI Host')).toBeTruthy();
    expect(within(engine).getByText('Asterisk Inbound Secret')).toBeTruthy();
  });
});
