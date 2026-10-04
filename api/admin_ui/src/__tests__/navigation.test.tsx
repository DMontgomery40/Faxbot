// The console's navigation: one table of areas and pages, who sees each page,
// an address per page, breadcrumbs, and the person's own menu.
import { describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import App from '../App';
import AdminAPIClient from '../api/client';
import ApiKeys from '../components/ApiKeys';
import DeliveryRoutes from '../components/DeliveryRoutes';
import Settings from '../components/Settings';
import { roleLine } from '../components/shell/UserMenu';
import type { AccessUserDetail, AuthMe } from '../api/types';
import {
  LEGACY_DESTINATIONS,
  NAVIGATION,
  destinationAddress,
  pageAddress,
  parseAddress,
  resolveAddress,
  visibleNavigation,
  type ConsoleNavigation,
  type LegacyDestination,
} from '../navigation';
import { ALL_PERMISSIONS, backend, server } from '../test/server';
import { settingsFixture, withDirections } from '../test/settingsFixture';

const everything = new Set(ALL_PERMISSIONS.map(([permission]) => permission));
const allScreens = { send: true, jobs: true, inbox: true, work: true };
const noScreens = { send: false, jobs: false, inbox: false, work: false };
const visible = (permissions: Iterable<string>, navigation: ConsoleNavigation = noScreens, pluginsEnabled = false) =>
  visibleNavigation(new Set(permissions), navigation, { pluginsEnabled });
const pagesOf = (areas: ReturnType<typeof visible>, area: string) => areas.find((a) => a.id === area)?.pages.map((p) => p.id) ?? [];

describe('the navigation table', () => {
  it('lists the eight areas in order for someone who may see everything', () => {
    const areas = visible(everything, allScreens, true);
    expect(areas.map((area) => area.label)).toEqual(
      ['Overview', 'Faxes', 'Numbers', 'Recipients', 'Providers', 'Costs', 'Access', 'System']);
    expect(pagesOf(areas, 'providers')).toEqual(
      ['sending', 'humblefax', 'efax', 'phaxio', 'sinch', 'signalwire', 'documo', 'trunk', 'freeswitch', 'change']);
    const system = areas.find((area) => area.id === 'system')!;
    expect(system.pages.filter((page) => page.group === 'Developer').map((page) => page.label))
      .toEqual(['API & SDKs', 'AI assistants', 'Terminal', 'Scripts & checks', 'Provider plugins']);
  });

  it('shows a fax operator their faxes and their own sessions only', () => {
    const areas = visible(['fax:send', 'fax:read', 'inbound:list', 'inbound:read'], { send: true, jobs: true, inbox: true });
    expect(areas.map((area) => area.id)).toEqual(['faxes', 'access']);
    expect(pagesOf(areas, 'faxes')).toEqual(['received', 'sent', 'send']);
    expect(pagesOf(areas, 'access')).toEqual(['sessions']);
  });

  it('keeps Sessions for everyone signed in, even with no permissions', () => {
    const areas = visible([]);
    expect(areas.map((area) => area.id)).toEqual(['access']);
    expect(pagesOf(areas, 'access')).toEqual(['sessions']);
  });

  it('shows Numbers to someone who may only read mailboxes', () => {
    const areas = visible(['mailboxes:read']);
    expect(pagesOf(areas, 'numbers')).toEqual(['list', 'mailboxes']);
    expect(areas.some((area) => area.id === 'providers')).toBe(false);
  });

  it('follows each page permission as the old tabs did', () => {
    expect(pagesOf(visible(['diagnostics:read']), 'overview')).toEqual(['overview']);
    expect(pagesOf(visible(['settings:read']), 'system')).toEqual(['security', 'storage', 'remote', 'api', 'assistants', 'plugins']);
    expect(pagesOf(visible(['settings:write']), 'system')).toEqual(['setup']);
    expect(pagesOf(visible(['host:terminal', 'logs:read']), 'system')).toEqual(['logs', 'terminal']);
    expect(pagesOf(visible(['keys:manage']), 'access')).toEqual(['keys', 'sessions']);
    expect(pagesOf(visible(['grants:read']), 'access')).toEqual(['who', 'sessions']);
    expect(pagesOf(visible(['audit:read']), 'system')).toEqual(['audit']);
  });

  it('lists Provider plugins whether or not plugins are on, so they can be turned on there', () => {
    expect(pagesOf(visible(['providers:read']), 'system')).toEqual(['plugins']);
    expect(pagesOf(visible(['providers:read'], noScreens, true), 'system')).toEqual(['plugins']);
  });

  it('gives every legacy destination a page that exists', () => {
    const all = new Set(NAVIGATION.flatMap((area) => area.pages.map((page) => `${area.id}/${page.id}`)));
    for (const destination of Object.keys(LEGACY_DESTINATIONS) as LegacyDestination[]) {
      expect(all.has(LEGACY_DESTINATIONS[destination])).toBe(true);
    }
    expect(destinationAddress('trunk')).toBe('#/providers/trunk');
    expect(destinationAddress('routes')).toBe('#/costs/spending');
    expect(destinationAddress('recipients/partners')).toBe('#/recipients/partners');
    expect(destinationAddress('access/keys?mine=1')).toBe('#/access/keys?mine=1');
  });
});

describe('page addresses', () => {
  const areas = visible(everything, allScreens);

  it('reads an area, a page and its query', () => {
    expect(parseAddress('#/providers/trunk')).toMatchObject({ area: 'providers', page: 'trunk' });
    expect(parseAddress('#/overview')).toMatchObject({ area: 'overview', page: null });
    expect(parseAddress('#/access/keys?mine=1')?.params.get('mine')).toBe('1');
    expect(parseAddress('')).toBeNull();
    expect(parseAddress('#settings')).toBeNull();
  });

  it('opens the named page, or the area first page when only the area is named', () => {
    expect(resolveAddress(areas, parseAddress('#/system/no-such-page'))?.address).toBe('#/overview');
    expect(resolveAddress(areas, parseAddress('#/providers/trunk'))?.address).toBe('#/providers/trunk');
    expect(resolveAddress(areas, parseAddress('#/providers'))?.address).toBe('#/providers/sending');
    expect(resolveAddress(areas, parseAddress('#/overview'))?.address).toBe('#/overview');
    expect(resolveAddress(areas, null)?.address).toBe('#/overview');
  });

  it('falls back to the first page this person may open', () => {
    const operator = visible(['fax:send'], { send: true, jobs: true, inbox: true });
    expect(resolveAddress(operator, parseAddress('#/system/security'))?.address).toBe('#/faxes/received');
  });
});

function grant(...permissions: string[]) {
  const admin = backend.state.principals.get('p_admin')!;
  admin.permissions = [...new Set([...admin.permissions, ...permissions])];
}

async function signIn() {
  render(<App />);
  await screen.findByRole('heading', { name: 'Sign in' });
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct horse' } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
  await screen.findByText('Ada Admin');
}

async function openPage(area: string, page: string) {
  const areaButton = await screen.findByRole('button', { name: area });
  if (areaButton.getAttribute('aria-expanded') !== 'true') fireEvent.click(areaButton);
  fireEvent.click(await screen.findByRole('link', { name: page }));
}

const crumbs = () => within(screen.getByRole('navigation', { name: 'You are here' }));

describe('the console shell', () => {
  it('opens the page a link names once the person signs in', async () => {
    window.history.replaceState(null, '', '/#/access/roles');
    await signIn();
    expect(await screen.findByRole('heading', { name: 'Roles' })).toBeTruthy();
    expect(window.location.hash).toBe('#/access/roles');
    expect(crumbs().getByRole('link', { name: 'Access' }).getAttribute('href')).toBe('#/access/users');
    expect(crumbs().getByText('Roles').getAttribute('aria-current')).toBe('page');
  });

  it('writes the page address on navigation, with the area and page in the breadcrumbs', async () => {
    await signIn();
    await openPage('Access', 'Groups');
    expect(window.location.hash).toBe('#/access/groups');
    expect(await screen.findByRole('heading', { name: 'Groups' })).toBeTruthy();
    expect(crumbs().getByRole('link', { name: 'Access' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Groups' }).getAttribute('aria-current')).toBe('page');
    // The area crumb opens the area's first page.
    fireEvent.click(crumbs().getByRole('link', { name: 'Access' }));
    expect(window.location.hash).toBe('#/access/users');
    expect(await screen.findByRole('heading', { name: 'Users' })).toBeTruthy();
  });

  it('follows Back and Forward through the address', async () => {
    await signIn();
    await openPage('Access', 'Roles');
    await screen.findByRole('heading', { name: 'Roles' });
    act(() => {
      window.history.replaceState(null, '', '#/access/groups');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(await screen.findByRole('heading', { name: 'Groups' })).toBeTruthy();
  });

  it('shows a page this person may open instead of one they may not, without adding history', async () => {
    window.history.replaceState(null, '', '/#/system/security');
    const before = window.history.length;
    await signIn();
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/received'));
    expect(window.history.length).toBe(before);
    expect(crumbs().getByRole('link', { name: 'Faxes' })).toBeTruthy();
  });

  it('opens every page at its own address, with its area and name above it', async () => {
    grant(...everything);
    backend.state.providerView = { plugins_enabled: true, install_enabled: false, active_outbound: 'phaxio', active_inbound: '' };
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(settingsFixture())),
      http.get('/admin/tunnel/status', () => HttpResponse.json({ enabled: false, provider: 'none', status: 'disabled' })),
    );
    await signIn();
    const areas = visibleNavigation(everything, { send: true, jobs: true, inbox: true }, { pluginsEnabled: true });
    const opened: string[] = [];
    for (const area of areas) {
      for (const page of area.pages) {
        if (page.link) continue;  // Add or change a provider opens the Setup wizard
        const address = pageAddress(area.id, page.id);
        act(() => { window.location.hash = address; });
        await waitFor(() => expect(window.location.hash).toBe(address));
        if (area.pages.length > 1) {
          await waitFor(() => expect(crumbs().getByText(page.label).getAttribute('aria-current')).toBe('page'));
          if (page.label !== area.label) expect(crumbs().getByRole('link', { name: area.label })).toBeTruthy();
        } else {
          expect(await screen.findByRole('heading', { name: area.label })).toBeTruthy();
        }
        opened.push(`${area.id}/${page.id}`);
      }
    }
    expect(opened).toHaveLength(42);
  }, 60000);

  it('keeps the old destination names working', async () => {
    grant('settings:read', 'diagnostics:read');
    await signIn();
    // The Overview Email delivery card used the Inbox destination.
    fireEvent.click(await screen.findByRole('button', { name: 'Email delivery' }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/received'));
  });
});

describe('the user menu', () => {
  it('shows the name and role, and My API keys opens Keys & phones with only my keys', async () => {
    backend.state.assignments.set('asg_admin', { id: 'asg_admin', subject: { kind: 'principal', id: 'p_admin' },
      role_id: 'role_fax_operator', resource_id: 'res_installation', version: 1 });
    backend.state.keys.set('key_mine', { id: 'key_mine', token: 'fbk_live_mine', principal_id: 'p_admin', name: 'My laptop', note: '',
      expires_at: null, created_at: '2026-10-01T12:00:00', last_used_at: null, revoked_at: null, pending_review: false,
      ceiling: [{ permission: 'fax:send', resource_id: 'res_installation' }], version: 1 });
    await signIn();
    expect((await screen.findByTestId('user-role')).textContent).toBe('Fax Operator');
    fireEvent.click(screen.getByTestId('user-menu-button'));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'My API keys' }));
    await waitFor(() => expect(window.location.hash).toBe('#/access/keys?mine=1'));
    expect(await screen.findByText('My laptop')).toBeTruthy();
    expect(screen.queryByText('Scanner')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }));
    await waitFor(() => expect(window.location.hash).toBe('#/access/keys'));
    expect(await screen.findByText('Scanner')).toBeTruthy();
  });

  it('offers My API keys only to people who manage keys', async () => {
    const admin = backend.state.principals.get('p_admin')!;
    admin.permissions = admin.permissions.filter((permission) => permission !== 'keys:manage');
    await signIn();
    fireEvent.click(screen.getByTestId('user-menu-button'));
    expect(await screen.findByRole('menuitem', { name: 'My sessions' })).toBeTruthy();
    expect(screen.queryByRole('menuitem', { name: 'My API keys' })).toBeNull();
    fireEvent.click(screen.getByRole('menuitem', { name: 'My sessions' }));
    await waitFor(() => expect(window.location.hash).toBe('#/access/sessions'));
  });

  it('saves the chosen appearance', async () => {
    await signIn();
    fireEvent.click(screen.getByTestId('user-menu-button'));
    fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Light' }));
    expect(window.localStorage.getItem('theme-mode')).toBe('light');
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Match my system' }));
    expect(window.localStorage.getItem('theme-mode')).toBe('system');
    expect(screen.getByRole('menuitemradio', { name: 'Match my system' }).getAttribute('aria-checked')).toBe('true');
  });

  it('changes the password in a dialog, without leaving the page', async () => {
    await signIn();
    fireEvent.click(screen.getByTestId('user-menu-button'));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Change password' }));
    const dialog = await screen.findByRole('dialog', { name: 'Change password' });
    fireEvent.change(within(dialog).getByLabelText('Current password'), { target: { value: 'correct horse' } });
    fireEvent.change(within(dialog).getByLabelText('New password'), { target: { value: 'a much better password' } });
    fireEvent.change(within(dialog).getByLabelText('Confirm new password'), { target: { value: 'a much better password' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Change password' }));
    expect(await within(dialog).findByText('Your password was changed.')).toBeTruthy();
    expect(backend.requestsTo('POST', '/auth/password')[0].body)
      .toEqual({ current_password: 'correct horse', password: 'a much better password' });
    expect(screen.getByText('Ada Admin')).toBeTruthy();
  });
});

describe('the role line', () => {
  const me = (overrides: Partial<AuthMe> = {}): AuthMe => ({
    principal: { id: 'p', kind: 'user', display_name: 'Pat', version: 1 }, source: 'session', password_change_required: false,
    policy_version: 1, permissions: [], session: null, can_enroll_owner: false, ...overrides,
  });
  const detail = (roles: Array<[string, 'installation' | 'mailbox']>, groups: string[] = []) => ({
    assignments: roles.map(([name, kind], index) => ({ id: `a${index}`, subject: { kind: 'principal', id: 'p', name: 'Pat' },
      role: { id: `r${index}`, name, builtin: true }, resource: { id: 'res', kind, name: 'Installation' }, version: 1 })),
    memberships: groups.map((group_name, index) => ({ membership_id: `m${index}`, group_id: `g${index}`, group_name, version: 1 })),
  }) as unknown as AccessUserDetail;

  it('names the owner, then installation roles, then other roles, then groups', () => {
    expect(roleLine(me({ is_owner: true }), null)).toBe('Owner');
    expect(roleLine(me(), detail([['Fax Operator', 'installation'], ['Viewer', 'mailbox']]))).toBe('Fax Operator');
    expect(roleLine(me(), detail([['Viewer', 'mailbox']]))).toBe('Viewer');
    expect(roleLine(me(), detail([], ['Front office']))).toBe('In the Front office group');
    expect(roleLine(me(), detail([], ['Front office', 'Billing']))).toBe('In the Front office and Billing groups');
    expect(roleLine(me({ principal: { id: 'i', kind: 'integration', display_name: 'Scanner', version: 1 } }), null)).toBe('Connected system');
    expect(roleLine(me(), null)).toBe('');
  });
});

const keyClient = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

describe('a page that shows part of the settings', () => {
  it('shows a provider page whether or not it is in use, with one sentence saying which', async () => {
    const data = settingsFixture();
    withDirections(data, 'phaxio', 'phaxio');
    server.use(http.get('/admin/settings', () => HttpResponse.json(data)));
    const { unmount } = render(<Settings client={keyClient()} sections={['phaxio']} title="Phaxio" />);
    expect((await screen.findByTestId('provider-use')).textContent).toBe('Faxbot sends and receives faxes through Phaxio.');
    expect(screen.getByRole('heading', { name: 'Phaxio' })).toBeTruthy();
    expect(screen.queryByText('Security Settings')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Export .env' })).toBeNull();
    unmount();
    render(<Settings client={keyClient()} sections={['sinch']} title="Sinch" />);
    expect((await screen.findByTestId('provider-use')).textContent)
      .toBe('Faxbot does not use Sinch now. To use it, choose Add or change a provider.');
    expect(screen.getByText('Sinch Project ID')).toBeTruthy();
  });

  it('offers Export and the recovery file only on Storage & retention', async () => {
    server.use(http.get('/admin/settings', () => HttpResponse.json(settingsFixture())));
    render(<Settings client={keyClient()} sections={['storage', 'advanced', 'backup']} title="Storage & retention" />);
    expect(await screen.findByRole('button', { name: 'Export .env' })).toBeTruthy();
    // One way to read the settings again, called Reload.
    expect(screen.getAllByRole('button', { name: 'Reload' })).toHaveLength(1);
    expect(screen.queryByText(/Load Settings/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull();
    expect(screen.getByText('Storage Configuration')).toBeTruthy();
    expect(screen.queryByText('Fax providers')).toBeNull();
  });
});

describe('one part of delivery routes on its own page', () => {
  it('loads and shows only that part', async () => {
    const asked: string[] = [];
    server.use(
      http.get('/routing/destinations', () => { asked.push('destinations'); return HttpResponse.json({ window_days: 30, destinations: [] }); }),
      http.get('/routing/rate-cards', () => { asked.push('rate-cards'); return HttpResponse.json({ cards: [] }); }),
    );
    render(<DeliveryRoutes client={keyClient()} canWrite={false} section="spending" />);
    expect(await screen.findByRole('heading', { name: 'Spending' })).toBeTruthy();
    expect(await screen.findByText('No faxes have been sent or received in the last 30 days.')).toBeTruthy();
    expect(screen.queryByText('Rate cards')).toBeNull();
    expect(asked).toEqual([]);
  });
});

describe('My API keys', () => {
  it('shows only the signed-in person keys and offers every key', async () => {
    await AdminAPIClient.login('admin', 'correct horse');
    const client = new AdminAPIClient({ kind: 'session', csrf: null });
    const me = await client.me();
    let showAll = 0;
    render(<ApiKeys client={client} me={me} onlyMine onShowAll={() => { showAll += 1; }} />);
    expect(await screen.findByText('You have not made any keys for yourself.')).toBeTruthy();
    expect(screen.getByTestId('only-my-keys').textContent).toContain('These are only the keys that belong to you.');
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }));
    expect(showAll).toBe(1);
  });
});
