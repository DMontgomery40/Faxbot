import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';
import AdminAPIClient, { TRANSPORT_REFUSED } from '../api/client';
import { backend } from '../test/server';

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

// Opens a page from the left panel: its area first, then the page's link.
async function openPage(area: string, page: string) {
  const areaButton = await screen.findByRole('button', { name: area });
  if (areaButton.getAttribute('aria-expanded') !== 'true') fireEvent.click(areaButton);
  fireEvent.click(await screen.findByRole('link', { name: page }));
}

async function signInWithPassword(login: string, password: string) {
  await screen.findByRole('heading', { name: 'Sign in' });
  type('Username', login);
  type('Password', password);
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('console sign-in', () => {
  it('boots to the sign-in screen, drops the stored legacy key and never replays it', async () => {
    window.localStorage.setItem('faxbot_admin_key', 'fbk_live_legacy');
    render(<App />);
    await screen.findByRole('heading', { name: 'Sign in' });
    expect(window.localStorage.getItem('faxbot_admin_key')).toBeNull();
    const boots = backend.requestsTo('GET', '/auth/me');
    expect(boots.length).toBeGreaterThan(0);
    for (const request of backend.state.requests) expect(request.headers['x-api-key']).toBeUndefined();
  });

  it('signs in with username and password, then signs out with the CSRF token', async () => {
    render(<App />);
    await signInWithPassword('admin', 'correct horse');
    expect(await screen.findByText('Ada Admin')).toBeTruthy();
    expect(backend.requestsTo('POST', '/auth/login')[0].body).toEqual({ login: 'admin', password: 'correct horse' });
    expect(screen.queryByText(/local only/i)).toBeNull();

    fireEvent.click(screen.getByTestId('user-menu-button'));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Sign out' }));
    await screen.findByRole('heading', { name: 'Sign in' });
    const logout = backend.requestsTo('POST', '/auth/logout');
    expect(logout).toHaveLength(1);
    expect(logout[0].headers['x-csrf-token']).toBe('csrf-1');
    expect(logout[0].headers['x-api-key']).toBeUndefined();
  });

  it("shows the server's message when the password is wrong", async () => {
    render(<App />);
    await signInWithPassword('admin', 'wrong');
    expect(await screen.findByText('Authentication required or credentials no longer valid.')).toBeTruthy();
  });

  it('signs in with an API key through a browser session', async () => {
    render(<App />);
    await screen.findByRole('heading', { name: 'Sign in' });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with API key' }));
    type('API key', 'fbk_live_scan_original');
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('Front desk scanner')).toBeTruthy();
    expect(backend.requestsTo('POST', '/auth/key-login')[0].body).toEqual({ api_key: 'fbk_live_scan_original' });
  });

  it('falls back to the key header when browser sessions are refused on this connection', async () => {
    backend.state.keyLoginStatus = { status: 403, detail: TRANSPORT_REFUSED };
    render(<App />);
    await screen.findByRole('heading', { name: 'Sign in' });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with API key' }));
    type('API key', 'fbk_live_scan_original');
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('Front desk scanner')).toBeTruthy();
    const keyed = backend.requestsTo('GET', '/auth/me').filter((r) => r.headers['x-api-key'] === 'fbk_live_scan_original');
    expect(keyed.length).toBeGreaterThan(0);
    const stored = Object.keys(window.localStorage).map((name) => window.localStorage.getItem(name) ?? '');
    expect(stored.some((value) => value.includes('fbk_live'))).toBe(false);
  });

  it('explains password sign-in refusal on an insecure connection in one sentence', async () => {
    render(<App />);
    await screen.findByRole('heading', { name: 'Sign in' });
    backend.state.requests.length = 0;
    const original = backend.state.principals.get('p_admin')!;
    original.enabled = true;
    // Simulate the transport gate for password sign-in.
    const { server } = await import('../test/server');
    const { http, HttpResponse } = await import('msw');
    server.use(http.post('/auth/login', () => HttpResponse.json({ detail: TRANSPORT_REFUSED }, { status: 403 })));
    type('Username', 'admin');
    type('Password', 'correct horse');
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('Password sign-in needs a secure (HTTPS) connection here. Sign in with an API key instead.')).toBeTruthy();
  });

  it('requires a new password before continuing, then re-reads the identity', async () => {
    render(<App />);
    await signInWithPassword('newbie', 'temporary-1');
    await screen.findByRole('heading', { name: 'Choose a new password' });
    type('Current password', 'temporary-1');
    type('New password', 'a much better password');
    type('Confirm new password', 'a much better password');
    const mesBefore = backend.requestsTo('GET', '/auth/me').length;
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }));
    expect(await screen.findByText('Nia New')).toBeTruthy();
    const change = backend.requestsTo('POST', '/auth/password');
    expect(change[0].body).toEqual({ current_password: 'temporary-1', password: 'a much better password' });
    expect(change[0].headers['x-csrf-token']).toBe('csrf-1');
    expect(backend.requestsTo('GET', '/auth/me').length).toBeGreaterThan(mesBefore);
  });

  it('stays signed in when one route refuses this kind of credential', async () => {
    const { server } = await import('../test/server');
    const { http, HttpResponse } = await import('msw');
    server.use(http.get('/admin/fax-jobs', () => HttpResponse.json({ detail: 'Unauthorized' }, { status: 401 })));
    render(<App />);
    await signInWithPassword('admin', 'correct horse');
    await screen.findByText('Ada Admin');
    const probesBefore = backend.requestsTo('GET', '/auth/me').length;
    await openPage('Faxes', 'Sent');
    await waitFor(() => expect(backend.requestsTo('GET', '/auth/me').length).toBeGreaterThan(probesBefore));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('heading', { name: 'Sign in' })).toBeNull();
    expect(screen.getByText('Ada Admin')).toBeTruthy();
  });

  it('returns to sign-in when the session ends', async () => {
    render(<App />);
    await signInWithPassword('admin', 'correct horse');
    await screen.findByText('Ada Admin');
    backend.state.session = null;
    // Send a fax reads the console context again on entry.
    await openPage('Faxes', 'Send a fax');
    await screen.findByRole('heading', { name: 'Sign in' });
    expect(screen.getByText('Your session has ended. Sign in again.')).toBeTruthy();
  });
});

describe('API client credentials', () => {
  it('re-reads /auth/me once and retries when the CSRF token is stale', async () => {
    await AdminAPIClient.login('admin', 'correct horse');
    const client = new AdminAPIClient({ kind: 'session', csrf: null });
    await client.me();
    backend.state.csrf = 'csrf-rotated';
    await client.createGroup({ name: 'Night shift', description: '', enabled: true });
    const posts = backend.requestsTo('POST', '/access/groups');
    expect(posts.map((r) => r.headers['x-csrf-token'])).toEqual(['csrf-1', 'csrf-rotated']);
    expect(posts[1].body).toEqual({ name: 'Night shift', description: '', enabled: true, expected_policy_version: 7 });
    expect(client.policyVersion).toBe(8);
  });

  it('never sends an empty X-API-Key and uses the key header in key mode', async () => {
    const client = new AdminAPIClient({ kind: 'key', key: 'fbk_live_scan_original' });
    const me = await client.me();
    expect(me.source).toBe('key');
    const empty = new AdminAPIClient({ kind: 'key', key: '' });
    await expect(empty.me({ quiet401: true })).rejects.toMatchObject({ status: 401 });
    const requests = backend.requestsTo('GET', '/auth/me');
    expect(requests[0].headers['x-api-key']).toBe('fbk_live_scan_original');
    expect('x-api-key' in requests[1].headers).toBe(false);
    await waitFor(() => expect(requests).toHaveLength(2));
  });
});

describe('A new installation with no owner yet', () => {
  const FIRST = 'This installation has no owner yet: sign in with the installation key (API_KEY in .env) to create the first owner.';

  it('asks for the installation key first and says why', async () => {
    backend.state.firstOwner = true;
    render(<App />);
    expect((await screen.findByTestId('first-owner')).textContent).toBe(FIRST);
    expect(screen.getByLabelText('Installation key')).toBeTruthy();
    expect(screen.queryByLabelText('Username')).toBeNull();
    type('Installation key', 'bootstrap-secret');
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText(/No owner account exists yet/)).toBeTruthy();
  });

  it('is the usual sign-in once an owner exists', async () => {
    render(<App />);
    await screen.findByRole('heading', { name: 'Sign in' });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByLabelText('Username')).toBeTruthy();
    expect(screen.queryByTestId('first-owner')).toBeNull();
  });

  it('ends creating the first owner at Setup when no fax provider is set up', async () => {
    backend.state.firstOwner = true;
    backend.state.providerView = { plugins_enabled: false, install_enabled: false, active_outbound: '', active_inbound: '' };
    render(<App />);
    await screen.findByLabelText('Installation key');
    type('Installation key', 'bootstrap-secret');
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Create the first owner' }));
    type('Username', 'owner');
    type('Display name', 'Olive Owner');
    fireEvent.click(screen.getByRole('button', { name: 'Create owner' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }));
    expect(await screen.findByRole('heading', { name: 'Setup Wizard' })).toBeTruthy();
  });
});
