import { describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminAPIClient from '../api/client';
import ResourceAccess, { INSTALLATION_WARNING } from '../components/ResourceAccess';
import { backend } from '../test/server';

async function signedInClient() {
  await AdminAPIClient.login('admin', 'correct horse');
  const client = new AdminAPIClient({ kind: 'session', csrf: null });
  const me = await client.me();
  return { client, me };
}

describe('resource access', () => {
  it('keeps the mailbox draft on a policy conflict, then creates it after Reload', async () => {
    const { client, me } = await signedInClient();
    render(<ResourceAccess client={client} me={me} />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Mailboxes' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Add mailbox' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add mailbox' });
    fireEvent.change(within(dialog).getByLabelText('Mailbox name'), { target: { value: 'Billing' } });
    await act(() => client.refreshPolicy()); // the dialog read the policy version when it opened

    backend.bumpPolicy(); // another administrator changed access meanwhile
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add mailbox' }));
    expect(await within(dialog).findByText('Access policy changed. Reload and try again.')).toBeTruthy();
    expect((within(dialog).getByLabelText('Mailbox name') as HTMLInputElement).value).toBe('Billing');
    expect(backend.requestsTo('POST', '/access/mailboxes')[0].body).toEqual({ label: 'Billing', enabled: true, expected_policy_version: 7 });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Reload' }));
    // Reload refreshes the policy version without discarding the draft.
    await waitFor(() => expect(client.policyVersion).toBe(8));
    await waitFor(() => expect(within(dialog).queryByText('Access policy changed. Reload and try again.')).toBeNull());
    expect((within(dialog).getByLabelText('Mailbox name') as HTMLInputElement).value).toBe('Billing');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add mailbox' }));
    expect(await screen.findByText('Billing')).toBeTruthy();
    const posts = backend.requestsTo('POST', '/access/mailboxes');
    expect(posts[posts.length - 1].body).toEqual({ label: 'Billing', enabled: true, expected_policy_version: 8 });
  });

  it('warns that installation-wide access covers every fax and mailbox', async () => {
    const { client, me } = await signedInClient();
    render(<ResourceAccess client={client} me={me} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Give access' }));
    const dialog = await screen.findByRole('dialog', { name: 'Give access' });
    fireEvent.change(within(dialog).getByLabelText('Where'), { target: { value: 'res_installation' } });
    expect(within(dialog).getByText(INSTALLATION_WARNING)).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Who'), { target: { value: 'group:grp_front' } });
    fireEvent.change(within(dialog).getByLabelText('Role'), { target: { value: 'role_fax_operator' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Give access' }));
    expect(await screen.findByText('Front office')).toBeTruthy();
    expect(backend.requestsTo('POST', '/access/assignments')[0].body).toEqual({
      subject: { kind: 'group', id: 'grp_front', version: 1 },
      role: { id: 'role_fax_operator', version: 1 },
      resource_id: 'res_installation',
      expected_policy_version: 7,
    });
  });
});
