// Access changes read the current policy version when their dialog opens, so
// "Access policy changed" appears only for edits made while it was open.
import { describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import type { ReactElement } from 'react';
import AdminAPIClient, { POLICY_CHANGED } from '../api/client';
import type { AuthMe } from '../api/types';
import ApiKeys from '../components/ApiKeys';
import Groups from '../components/Groups';
import ResourceAccess from '../components/ResourceAccess';
import Roles from '../components/Roles';
import Sessions from '../components/Sessions';
import Users from '../components/Users';
import { toServerTime } from '../api/time';
import { backend, server } from '../test/server';

async function signedInClient() {
  await AdminAPIClient.login('admin', 'correct horse');
  const client = new AdminAPIClient({ kind: 'session', csrf: null });
  const me = await client.me();
  return { client, me };
}

type Scenario = {
  name: string;
  screen: (client: AdminAPIClient, me: AuthMe) => ReactElement;
  open: () => Promise<void>;
  dialog: string;
  fill: (dialog: HTMLElement) => void;
  submit: string;
  done: () => Promise<unknown>;
};

const click = async (name: string, role: 'button' | 'tab' = 'button') => { fireEvent.click(await screen.findByRole(role, { name })); };

const scenarios: Scenario[] = [
  { name: 'Users', screen: (c, m) => <Users client={c} me={m} />, open: () => click('Add person'), dialog: 'Add person',
    fill: (d) => {
      fireEvent.change(within(d).getByLabelText('Username'), { target: { value: 'grace' } });
      fireEvent.change(within(d).getByLabelText('Display name'), { target: { value: 'Grace Hopper' } });
    },
    submit: 'Add person', done: () => screen.findByRole('dialog', { name: 'Person added' }) },
  { name: 'Groups', screen: (c, m) => <Groups client={c} me={m} />, open: () => click('Create group'), dialog: 'Create group',
    fill: (d) => fireEvent.change(within(d).getByLabelText('Group name'), { target: { value: 'Billing' } }),
    submit: 'Create group', done: () => screen.findByText('Billing') },
  { name: 'Roles', screen: (c, m) => <Roles client={c} me={m} />, open: () => click('Create role'), dialog: 'Create role',
    fill: (d) => {
      fireEvent.change(within(d).getByLabelText('Role name'), { target: { value: 'Front desk' } });
      fireEvent.click(within(d).getByLabelText('Send faxes'));
    },
    submit: 'Create role', done: () => screen.findByText('Front desk') },
  { name: 'Access', screen: (c, m) => <ResourceAccess client={c} me={m} />, open: () => click('Give access'), dialog: 'Give access',
    fill: (d) => {
      fireEvent.change(within(d).getByLabelText('Where'), { target: { value: 'res_installation' } });
      fireEvent.change(within(d).getByLabelText('Who'), { target: { value: 'group:grp_front' } });
      fireEvent.change(within(d).getByLabelText('Role'), { target: { value: 'role_fax_operator' } });
    },
    submit: 'Give access', done: () => screen.findByText('Front office') },
  { name: 'Mailboxes', screen: (c, m) => <ResourceAccess client={c} me={m} />,
    open: async () => { await click('Mailboxes', 'tab'); await click('Add mailbox'); }, dialog: 'Add mailbox',
    fill: (d) => fireEvent.change(within(d).getByLabelText('Mailbox name'), { target: { value: 'Billing' } }),
    submit: 'Add mailbox', done: () => screen.findByText('Billing') },
  { name: 'Fax numbers', screen: (c, m) => <ResourceAccess client={c} me={m} />,
    open: async () => { await click('Fax numbers', 'tab'); await click('Add number'); }, dialog: 'Add fax number',
    fill: (d) => {
      fireEvent.change(within(d).getByLabelText('Fax number'), { target: { value: '+15550100001' } });
      fireEvent.change(within(d).getByLabelText('Mailbox'), { target: { value: 'mbx_main' } });
    },
    submit: 'Add number', done: () => screen.findByText('+15550100001') },
  { name: 'Keys', screen: (c, m) => <ApiKeys client={c} me={m} />, open: () => click('Create key'), dialog: 'Create key',
    fill: (d) => {
      fireEvent.change(within(d).getByLabelText('Belongs to'), { target: { value: 'p_scanner' } });
      fireEvent.change(within(d).getByLabelText('Name'), { target: { value: 'Lobby scanner' } });
    },
    submit: 'Create key', done: () => screen.findByRole('dialog', { name: 'Key created' }) },
  { name: 'Key replacement', screen: (c, m) => <ApiKeys client={c} me={m} />, open: () => click('Rotate Scanner'), dialog: 'Replace this key?',
    fill: () => undefined, submit: 'Replace key', done: () => screen.findByRole('dialog', { name: 'Key replaced' }) },
  { name: 'Sessions', screen: (c, m) => <Sessions client={c} me={m} />, open: () => click('Sign out here'), dialog: 'Sign out of this session?',
    fill: () => undefined, submit: 'Sign out',
    done: () => waitFor(() => expect(backend.requestsTo('POST', '/auth/sessions/sess_current/revoke').length
      + backend.requestsTo('POST', '/access/sessions/sess_current/revoke').length).toBe(1)) },
];

describe('policy version refresh', () => {
  it.each(scenarios)('$name: a change made by someone else before the dialog opened does not block the edit', async (scenario) => {
    const { client, me } = await signedInClient();
    render(scenario.screen(client, me));
    backend.bumpPolicy(); // someone else changed access after this screen loaded
    await scenario.open();
    const dialog = await screen.findByRole('dialog', { name: scenario.dialog });
    scenario.fill(dialog);
    fireEvent.click(within(dialog).getByRole('button', { name: scenario.submit }));
    await scenario.done();
    expect(screen.queryByText(POLICY_CHANGED)).toBeNull();
  });

  it.each(scenarios.filter((s) => ['Users', 'Keys', 'Sessions', 'Fax numbers'].includes(s.name)))(
    '$name: a change made by someone else while the dialog is open is still refused', async (scenario) => {
      const { client, me } = await signedInClient();
      render(scenario.screen(client, me));
      await scenario.open();
      const dialog = await screen.findByRole('dialog', { name: scenario.dialog });
      scenario.fill(dialog);
      await act(() => client.refreshPolicy()); // the dialog's own refresh has finished
      backend.bumpPolicy();
      fireEvent.click(within(dialog).getByRole('button', { name: scenario.submit }));
      expect(await within(dialog).findByText(POLICY_CHANGED)).toBeTruthy();
    });

  it('picks up the policy change from pairing a phone before the next edit', async () => {
    const expires = toServerTime(new Date(Date.now() + 5 * 60 * 1000));
    server.use(http.post('/admin/tunnel/pair', () => {
      backend.bumpPolicy(); // issuing a pairing code changes access policy
      return HttpResponse.json({ code: '482913', expires_at: expires });
    }));
    const { client } = await signedInClient();
    await client.createTunnelPairing();
    await waitFor(() => expect(client.policyVersion).toBe(8));
  });
});

describe('Keys: pair a phone', () => {
  it('opens the pairing dialog from Keys and reloads the keys when it closes', async () => {
    const expires = toServerTime(new Date(Date.now() + 5 * 60 * 1000));
    server.use(http.post('/admin/tunnel/pair', () => HttpResponse.json({ code: '551200', expires_at: expires })));
    const { client, me } = await signedInClient();
    render(<ApiKeys client={client} me={me} />);
    await screen.findByText('Scanner');
    const loads = backend.requestsTo('GET', '/access/keys').length;
    fireEvent.click(screen.getByRole('button', { name: 'Pair a phone' }));
    const dialog = await screen.findByRole('dialog', { name: 'Pair a phone' });
    expect((await within(dialog).findByTestId('pairing-code')).textContent).toBe('551200');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(backend.requestsTo('GET', '/access/keys').length).toBe(loads + 1));
  });

  it('does not offer pairing without permission to pair devices', async () => {
    const { client, me } = await signedInClient();
    render(<ApiKeys client={client} me={{ ...me, permissions: me.permissions.filter((p) => p !== 'tunnels:pair') }} />);
    await screen.findByText('Scanner');
    expect(screen.queryByRole('button', { name: 'Pair a phone' })).toBeNull();
  });

  it('shows when each key was last used', async () => {
    backend.state.keys.get('key_scan')!.last_used_at = '2026-10-03T09:30:00';
    const { client, me } = await signedInClient();
    render(<ApiKeys client={client} me={me} />);
    const row = (await screen.findByText('Scanner')).closest('tr')!;
    expect(within(row).getByText(new Date('2026-10-03T09:30:00Z').toLocaleString())).toBeTruthy();
  });
});
