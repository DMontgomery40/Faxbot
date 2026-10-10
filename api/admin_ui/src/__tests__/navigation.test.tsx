// The console's navigation: one table of six areas and their pages, who sees each page,
// an address per page, breadcrumbs, Send a fax, the states for moved, unknown and
// forbidden addresses, and the person's own menu. Every old address is checked one by
// one in navigationCoverage.test.tsx.
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
  resolvesTo,
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
const groupsOf = (areas: ReturnType<typeof visible>, area: string) =>
  [...new Set(areas.find((a) => a.id === area)?.pages.map((p) => p.group ?? '') ?? [])];

describe('the navigation table', () => {
  it('lists the six areas in order for someone who may see everything, long areas under group headings', () => {
    // Expected needs work:read, which the test server's principals do not carry.
    const areas = visible([...everything, 'work:read'], allScreens, true);
    expect(areas.map((area) => area.label)).toEqual(
      ['Overview', 'Savings & optimization', 'Faxes', 'Delivery setup', 'Recipients', 'Administration']);
    expect(areas.map((area) => area.id)).toEqual(['overview', 'savings', 'faxes', 'delivery', 'recipients', 'admin']);
    expect(pagesOf(areas, 'savings')).toEqual(
      ['capabilities', 'opportunities', 'facts', 'results', 'spending', 'charges', 'invoices', 'prices']);
    expect(pagesOf(areas, 'faxes')).toEqual(['received', 'sent', 'send', 'expected', 'forms', 'cases']);
    expect(pagesOf(areas, 'delivery')).toEqual(['numbers', 'mailboxes', 'moves', 'blocked', 'identity', 'email', 'connectors',
      'connections', 'rules', 'humblefax', 'efax', 'phaxio', 'sinch', 'signalwire', 'documo', 'trunk', 'change']);
    expect(groupsOf(areas, 'delivery')).toEqual(['Numbers & mailboxes', 'Documents in and out', 'Connections']);
    expect(pagesOf(areas, 'recipients')).toEqual(['list', 'partners']);
    expect(pagesOf(areas, 'admin')).toEqual(['users', 'groups', 'roles', 'who', 'keys', 'sessions',
      'setup', 'security', 'retention', 'analysis', 'npi', 'health', 'audit', 'logs',
      'api', 'assistants', 'terminal', 'scripts', 'plugins']);
    expect(groupsOf(areas, 'admin')).toEqual(['People & access', 'Installation', 'Monitoring', 'Developer']);
    const admin = areas.find((area) => area.id === 'admin')!;
    expect(admin.pages.filter((page) => page.group === 'Developer').map((page) => page.label))
      .toEqual(['API & SDKs', 'AI assistants', 'Terminal', 'Scripts & checks', 'Provider plugins']);
    // The 52 entries the console had, and Capabilities.
    expect(NAVIGATION.flatMap((area) => area.pages)).toHaveLength(53);
  });

  it('shows a fax operator their faxes and their own sessions only', () => {
    const areas = visible(['fax:send', 'fax:read', 'inbound:list', 'inbound:read'], { send: true, jobs: true, inbox: true });
    expect(areas.map((area) => area.id)).toEqual(['faxes', 'admin']);
    expect(pagesOf(areas, 'faxes')).toEqual(['received', 'sent', 'send', 'forms']);
    expect(pagesOf(areas, 'admin')).toEqual(['sessions']);
  });

  it('keeps Sessions for everyone signed in, even with no permissions', () => {
    const areas = visible([]);
    expect(areas.map((area) => area.id)).toEqual(['admin']);
    expect(pagesOf(areas, 'admin')).toEqual(['sessions']);
  });

  it('shows Numbers and Mailboxes to someone who may only read mailboxes', () => {
    const areas = visible(['mailboxes:read']);
    expect(pagesOf(areas, 'delivery')).toEqual(['numbers', 'mailboxes']);
    expect(areas.some((area) => area.id === 'savings')).toBe(false);
  });

  it('follows each page permission as the old pages did', () => {
    expect(pagesOf(visible(['diagnostics:read']), 'overview')).toEqual(['overview']);
    expect(pagesOf(visible(['diagnostics:read']), 'admin')).toEqual(['sessions', 'health', 'api']);
    expect(pagesOf(visible(['settings:read']), 'admin'))
      .toEqual(['sessions', 'security', 'retention', 'analysis', 'npi', 'api', 'assistants', 'plugins']);
    expect(pagesOf(visible(['settings:write']), 'admin')).toEqual(['sessions', 'setup']);
    expect(pagesOf(visible(['settings:write']), 'delivery')).toEqual(['change']);
    expect(pagesOf(visible(['host:terminal', 'logs:read']), 'admin')).toEqual(['sessions', 'logs', 'terminal']);
    expect(pagesOf(visible(['keys:manage']), 'admin')).toEqual(['keys', 'sessions']);
    expect(pagesOf(visible(['grants:read']), 'admin')).toEqual(['who', 'sessions']);
    expect(pagesOf(visible(['audit:read']), 'admin')).toEqual(['sessions', 'audit']);
    expect(pagesOf(visible(['providers:write']), 'admin')).toEqual(['sessions', 'scripts']);
  });

  it('lists Provider plugins whether or not plugins are on, so they can be turned on there', () => {
    expect(pagesOf(visible(['providers:read']), 'admin')).toEqual(['sessions', 'plugins']);
    expect(pagesOf(visible(['providers:read'], noScreens, true), 'admin')).toEqual(['sessions', 'plugins']);
  });

  it('gives every legacy destination a page that exists', () => {
    const all = new Set(NAVIGATION.flatMap((area) => area.pages.map((page) => `${area.id}/${page.id}`)));
    for (const destination of Object.keys(LEGACY_DESTINATIONS) as LegacyDestination[]) {
      expect(all.has(LEGACY_DESTINATIONS[destination])).toBe(true);
    }
    expect(destinationAddress('trunk')).toBe('#/delivery/trunk');
    expect(destinationAddress('routes')).toBe('#/savings/spending');
    expect(destinationAddress('recipients/partners')).toBe('#/recipients/partners');
    expect(destinationAddress('admin/keys?mine=1')).toBe('#/admin/keys?mine=1');
  });

  it('writes an old address a screen still uses as its new one, with its query', () => {
    expect(destinationAddress('access/keys?mine=1')).toBe('#/admin/keys?mine=1');
    expect(destinationAddress('costs/savings?part=sslfax')).toBe('#/savings/results?part=sslfax');
    expect(destinationAddress('providers/trunk')).toBe('#/delivery/trunk');
    expect(destinationAddress('recipients/cases')).toBe('#/faxes/cases');
    expect(destinationAddress('faxes/work')).toBe('#/faxes/received?show=waiting');
  });

  it('tells a check where any address leads, whoever is looking', () => {
    expect(resolvesTo('costs/recommendations?section=plans')?.page.id).toBe('opportunities');
    expect(resolvesTo('savings/capabilities')?.area.id).toBe('savings');
    expect(resolvesTo('providers/change')?.page.id).toBe('setup');
    expect(resolvesTo('setup')?.area.id).toBe('admin');
    expect(resolvesTo('costs')?.page.id).toBe('spending');
    expect(resolvesTo('costs/no-such-page')).toBeNull();
    expect(resolvesTo('nowhere/at-all')).toBeNull();
  });
});

describe('page addresses', () => {
  const areas = visible(everything, allScreens);

  it('reads an area, a page and its query', () => {
    expect(parseAddress('#/delivery/trunk')).toMatchObject({ area: 'delivery', page: 'trunk' });
    expect(parseAddress('#/overview')).toMatchObject({ area: 'overview', page: null });
    expect(parseAddress('#/admin/keys?mine=1')?.params.get('mine')).toBe('1');
    expect(parseAddress('')).toBeNull();
    expect(parseAddress('#settings')).toBeNull();
  });

  it('opens the named page, or the area first page when only the area is named', () => {
    expect(resolveAddress(areas, parseAddress('#/delivery/trunk'))).toMatchObject({ kind: 'page', address: '#/delivery/trunk' });
    expect(resolveAddress(areas, parseAddress('#/delivery'))?.address).toBe('#/delivery/numbers');
    expect(resolveAddress(areas, parseAddress('#/savings'))?.address).toBe('#/savings/capabilities');
    expect(resolveAddress(areas, parseAddress('#/overview'))?.address).toBe('#/overview');
    expect(resolveAddress(areas, null)?.address).toBe('#/overview');
  });

  it('opens a former area at the first of its old pages this person may open', () => {
    expect(resolveAddress(areas, parseAddress('#/providers'))).toMatchObject({ address: '#/delivery/connections', movedFrom: 'Providers' });
    expect(resolveAddress(areas, parseAddress('#/costs'))?.address).toBe('#/savings/spending');
    expect(resolveAddress(areas, parseAddress('#/system'))?.address).toBe('#/admin/setup');
    expect(resolveAddress(visible(['logs:read']), parseAddress('#/system'))?.address).toBe('#/admin/logs');
    expect(resolveAddress(visible([]), parseAddress('#/access'))?.address).toBe('#/admin/sessions');
    expect(resolveAddress(visible([]), parseAddress('#/costs'))?.kind).toBe('forbidden');
  });

  it('says an address names no page, or a page this person may not open, instead of opening another page', () => {
    expect(resolveAddress(areas, parseAddress('#/admin/no-such-page'))).toEqual({ kind: 'unknown', address: '#/admin/no-such-page' });
    expect(resolveAddress(areas, parseAddress('#/nowhere'))?.kind).toBe('unknown');
    expect(resolveAddress(areas, parseAddress('#/system/no-such-page'))?.kind).toBe('unknown');
    const operator = visible(['fax:send'], { send: true, jobs: true, inbox: true });
    expect(resolveAddress(operator, parseAddress('#/admin/security'))).toEqual({ kind: 'forbidden', address: '#/admin/security' });
    expect(resolveAddress(operator, parseAddress('#/system/security'))).toEqual({ kind: 'forbidden', address: '#/system/security' });
    expect(resolveAddress(operator, parseAddress('#/savings'))?.kind).toBe('forbidden');
  });
});

function grant(...permissions: string[]) {
  const admin = backend.state.principals.get('p_admin')!;
  admin.permissions = [...new Set([...admin.permissions, ...permissions])];
}

function revoke(...permissions: string[]) {
  const admin = backend.state.principals.get('p_admin')!;
  admin.permissions = admin.permissions.filter((permission) => !permissions.includes(permission));
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
const panel = () => within(screen.getByRole('navigation', { name: 'Console' }));

describe('the console shell', () => {
  it('opens the page a link names once the person signs in', async () => {
    window.history.replaceState(null, '', '/#/admin/roles');
    await signIn();
    expect(await screen.findByRole('heading', { name: 'Roles' })).toBeTruthy();
    expect(window.location.hash).toBe('#/admin/roles');
    expect(crumbs().getByRole('link', { name: 'Administration' }).getAttribute('href')).toBe('#/admin/users');
    expect(crumbs().getByRole('link', { name: 'People & access' }).getAttribute('href')).toBe('#/admin/users');
    expect(crumbs().getByText('Roles').getAttribute('aria-current')).toBe('page');
    expect(screen.queryByTestId('moved-notice')).toBeNull();
  });

  it('writes the page address on navigation, with the area, group and page in the breadcrumbs', async () => {
    await signIn();
    await openPage('Administration', 'Groups');
    expect(window.location.hash).toBe('#/admin/groups');
    expect(await screen.findByRole('heading', { name: 'Groups' })).toBeTruthy();
    expect(crumbs().getByRole('link', { name: 'Administration' })).toBeTruthy();
    expect(crumbs().getByRole('link', { name: 'People & access' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Groups' }).getAttribute('aria-current')).toBe('page');
    // The area crumb opens the area's first page.
    fireEvent.click(crumbs().getByRole('link', { name: 'Administration' }));
    expect(window.location.hash).toBe('#/admin/users');
    expect(await screen.findByRole('heading', { name: 'Users' })).toBeTruthy();
  });

  it('keeps the area of the current page open and closes the others when the page changes area', async () => {
    await signIn();
    await openPage('Administration', 'Groups');
    await openPage('Faxes', 'Sent');
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/sent'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Administration' }).getAttribute('aria-expanded')).toBe('false'));
    expect(screen.getByRole('button', { name: 'Faxes' }).getAttribute('aria-expanded')).toBe('true');
  });

  it('follows Back and Forward through the address', async () => {
    await signIn();
    await openPage('Administration', 'Roles');
    await screen.findByRole('heading', { name: 'Roles' });
    act(() => {
      window.history.replaceState(null, '', '#/admin/groups');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(await screen.findByRole('heading', { name: 'Groups' })).toBeTruthy();
  });

  it('opens an old bookmark at the new page with its query, says once where it moved from, and adds no history', async () => {
    grant('settings:read');
    window.history.replaceState(null, '', '/#/costs/savings?part=sslfax');
    const before = window.history.length;
    await signIn();
    await waitFor(() => expect(window.location.hash).toBe('#/savings/results?part=sslfax'));
    expect(window.history.length).toBe(before);
    expect((await screen.findByTestId('moved-notice')).textContent)
      .toBe('You opened an old link to Costs › Savings; this is its new home.');
    expect(crumbs().getByText('Savings results').getAttribute('aria-current')).toBe('page');
    // Moving on clears the notice.
    await openPage('Savings & optimization', 'Spending');
    await waitFor(() => expect(window.location.hash).toBe('#/savings/spending'));
    expect(screen.queryByTestId('moved-notice')).toBeNull();
  });

  it('never shows the moved notice for a link inside the console', async () => {
    grant('settings:read', 'settings:write');
    window.history.replaceState(null, '', '/#/savings/results');
    await signIn();
    fireEvent.click(await screen.findByRole('button', { name: 'AI analysis settings' }));
    await waitFor(() => expect(window.location.hash).toBe('#/admin/analysis'));
    expect(crumbs().getByText('AI analysis').getAttribute('aria-current')).toBe('page');
    expect(screen.queryByTestId('moved-notice')).toBeNull();
  });

  it('opens the old Work address as Received, waiting for an owner', async () => {
    window.history.replaceState(null, '', '/#/faxes/work');
    await signIn();
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/received?show=waiting'));
    expect((await screen.findByTestId('moved-notice')).textContent).toContain('Faxes › Work');
  });

  it('shows its own state for a page this person may not open, without opening another page or changing the address', async () => {
    window.history.replaceState(null, '', '/#/system/security?section=secret-value');
    const before = window.history.length;
    await signIn();
    const state = await screen.findByTestId('address-forbidden');
    expect(within(state).getByRole('heading', { name: 'Not available to you' })).toBeTruthy();
    expect(state.textContent).toContain('Your account does not have access to this page.');
    expect(document.body.textContent).not.toContain('secret-value');
    expect(window.location.hash).toBe('#/system/security?section=secret-value');
    expect(window.history.length).toBe(before);
    expect(screen.queryByRole('navigation', { name: 'You are here' })).toBeNull();
    // The way on is the first page this person may open.
    fireEvent.click(within(state).getByRole('link', { name: 'Go to Received' }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/received'));
    expect(screen.queryByTestId('address-forbidden')).toBeNull();
  });

  it('shows its own state for an address that names no page', async () => {
    window.history.replaceState(null, '', '/#/admin/no-such-page');
    await signIn();
    const state = await screen.findByTestId('address-unknown');
    expect(within(state).getByRole('heading', { name: 'Page not found' })).toBeTruthy();
    expect(state.textContent).toContain('There is no page at this address.');
    expect(window.location.hash).toBe('#/admin/no-such-page');
    // The panel stays, with nothing marked as the current page.
    expect(panel().queryAllByRole('link').filter((link) => link.getAttribute('aria-current') === 'page')).toEqual([]);
  });

  it('opens the first page this person may open when the address names nothing', async () => {
    window.history.replaceState(null, '', '/#settings');
    await signIn();
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/received'));
    expect(screen.queryByTestId('moved-notice')).toBeNull();
  });

  it('keeps Send a fax at the top of the panel for people who may send, and only once', async () => {
    await signIn();
    const send = await screen.findByRole('link', { name: 'Send a fax' });
    expect(send.getAttribute('href')).toBe('#/faxes/send');
    expect(screen.getAllByRole('link', { name: 'Send a fax' })).toHaveLength(1);
    fireEvent.click(send);
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/send'));
    expect(screen.getByRole('link', { name: 'Send a fax' }).getAttribute('aria-current')).toBe('page');
    expect(crumbs().getByRole('link', { name: 'Faxes' })).toBeTruthy();
  });

  it('keeps Send a fax in the phone bar, and in the menu drawer while it is open', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({ ...original(query), matches: query.includes('max-width') })) as typeof window.matchMedia;
    try {
      window.history.replaceState(null, '', '/#/admin/roles');
      // On a phone the person's own menu is in the drawer, so wait for the page instead of their name.
      render(<App />);
      await screen.findByRole('heading', { name: 'Sign in' });
      fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } });
      fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct horse' } });
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
      expect(await screen.findByRole('heading', { name: 'Roles' })).toBeTruthy();
      expect((await screen.findAllByRole('link', { name: 'Send a fax' }))).toHaveLength(1);
      fireEvent.click(screen.getByRole('button', { name: 'open navigation' }));
      // The drawer opens on the current page's area, with Send a fax at its top instead of in the bar.
      const drawerPanel = await screen.findByRole('navigation', { name: 'Console' });
      expect(within(drawerPanel).getByRole('link', { name: 'Roles' }).getAttribute('aria-current')).toBe('page');
      await waitFor(() => expect(screen.getAllByRole('link', { name: 'Send a fax' })).toHaveLength(1));
      fireEvent.click(screen.getByRole('link', { name: 'Send a fax' }));
      await waitFor(() => expect(window.location.hash).toBe('#/faxes/send'));
    } finally {
      window.matchMedia = original;
    }
  });

  it('offers no Send a fax to people who may not send', async () => {
    revoke('fax:send');
    await signIn();
    await screen.findByRole('button', { name: 'Faxes' });
    expect(screen.queryByRole('link', { name: 'Send a fax' })).toBeNull();
    expect(screen.queryByTestId('send-action')).toBeNull();
  });

  it('opens every page at its own address, with its area, group and name above it', async () => {
    grant(...everything);
    backend.state.providerView = { plugins_enabled: true, install_enabled: false, active_outbound: 'phaxio', active_inbound: '' };
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(settingsFixture())),
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
          if (page.group) expect(crumbs().getByRole('link', { name: page.group })).toBeTruthy();
        } else {
          expect(await screen.findByRole('heading', { name: area.label })).toBeTruthy();
        }
        opened.push(`${area.id}/${page.id}`);
      }
    }
    expect(opened).toEqual(expect.arrayContaining(['delivery/moves', 'admin/analysis', 'savings/opportunities', 'savings/capabilities']));
    expect(new Set(opened).size).toBe(opened.length);
    expect(opened).not.toContain('admin/remote');
    expect(opened).not.toContain('delivery/freeswitch');
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
    await waitFor(() => expect(window.location.hash).toBe('#/admin/keys?mine=1'));
    expect(await screen.findByText('My laptop')).toBeTruthy();
    expect(screen.queryByText('Scanner')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }));
    await waitFor(() => expect(window.location.hash).toBe('#/admin/keys'));
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
    await waitFor(() => expect(window.location.hash).toBe('#/admin/sessions'));
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
    expect(screen.queryByRole('button', { name: 'Export settings' })).toBeNull();
    unmount();
    render(<Settings client={keyClient()} sections={['sinch']} title="Sinch" />);
    expect((await screen.findByTestId('provider-use')).textContent)
      .toBe('Faxbot does not use Sinch now. To use it, choose Add or change a provider.');
    expect(screen.getByText('Sinch Project ID')).toBeTruthy();
  });

  it('offers Export and the recovery file only on Storage & retention', async () => {
    server.use(http.get('/admin/settings', () => HttpResponse.json(settingsFixture())));
    render(<Settings client={keyClient()} sections={['storage', 'advanced', 'backup']} title="Storage & retention" />);
    expect(await screen.findByRole('button', { name: 'Export settings' })).toBeTruthy();
    // One way to read the settings again, called Reload.
    expect(screen.getAllByRole('button', { name: 'Reload' })).toHaveLength(1);
    expect(screen.queryByText(/Load Settings/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull();
    expect(screen.getByText('Where faxes are kept')).toBeTruthy();
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
