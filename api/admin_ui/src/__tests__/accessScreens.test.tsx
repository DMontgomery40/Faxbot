import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import AdminAPIClient from '../api/client';
import Users from '../components/Users';
import Roles from '../components/Roles';
import Groups from '../components/Groups';
import Sessions from '../components/Sessions';
import { backend } from '../test/server';

async function signedInClient() {
  await AdminAPIClient.login('admin', 'correct horse');
  const client = new AdminAPIClient({ kind: 'session', csrf: null });
  const me = await client.me();
  return { client, me };
}

describe('access screens', () => {
  it('adds a person and shows the temporary password once', async () => {
    const { client, me } = await signedInClient();
    render(<Users client={client} me={me} />);
    expect(await screen.findByText('Ada Admin')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add person' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add person' });
    fireEvent.change(within(dialog).getByLabelText('Username'), { target: { value: 'grace' } });
    fireEvent.change(within(dialog).getByLabelText('Display name'), { target: { value: 'Grace Hopper' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add person' }));
    const reveal = await screen.findByRole('dialog', { name: 'Person added' });
    expect(within(reveal).getByTestId('secret-value').textContent).toBe('temp-pass-1');
    expect(backend.requestsTo('POST', '/access/users')[0].body).toEqual({
      login: 'grace', display_name: 'Grace Hopper', enabled: true, expected_policy_version: 7,
    });
  });

  it('shows built-in roles read-only and creates a custom role from grantable permissions', async () => {
    const { client, me } = await signedInClient();
    render(<Roles client={client} me={me} />);
    expect(await screen.findByText('Fax Operator')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Edit Owner' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Create role' }));
    const dialog = await screen.findByRole('dialog', { name: 'Create role' });
    fireEvent.change(within(dialog).getByLabelText('Role name'), { target: { value: 'Front desk' } });
    // Not grantable by this administrator, so not offered.
    expect(within(dialog).queryByLabelText('Use the server terminal')).toBeNull();
    fireEvent.click(within(dialog).getByLabelText('Send faxes'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create role' }));
    expect(await screen.findByText('Front desk')).toBeTruthy();
    expect(backend.requestsTo('POST', '/access/roles')[0].body).toEqual({
      name: 'Front desk', description: '', permissions: ['fax:send'], enabled: true, expected_policy_version: 7,
    });
  });

  it('adds a member to a group with both versions', async () => {
    const { client, me } = await signedInClient();
    render(<Groups client={client} me={me} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Members of Front office' }));
    const select = await screen.findByLabelText('Add a member');
    fireEvent.change(select, { target: { value: 'p_scanner' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(await screen.findByRole('button', { name: 'Remove Front desk scanner' })).toBeTruthy();
    expect(backend.requestsTo('POST', '/access/groups/grp_front/members')[0].body).toEqual({
      principal_id: 'p_scanner', principal_version: 3, group_version: 1, expected_policy_version: 7,
    });
  });

  it('lists your own sessions', async () => {
    const { client, me } = await signedInClient();
    render(<Sessions client={client} me={me} />);
    expect(await screen.findByText('This session')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sign out here' })).toBeTruthy();
  });
});
