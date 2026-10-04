import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminAPIClient from '../api/client';
import ApiKeys, { lastUsedText } from '../components/ApiKeys';
import { formatServerTime, toServerTime } from '../api/time';
import { backend } from '../test/server';

async function signedInClient() {
  await AdminAPIClient.login('admin', 'correct horse');
  const client = new AdminAPIClient({ kind: 'session', csrf: null });
  const me = await client.me();
  return { client, me };
}

describe('API keys', () => {
  it('shows a rotated key once in its own dialog, and Create key then opens an empty form', async () => {
    const { client, me } = await signedInClient();
    render(<ApiKeys client={client} me={me} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Rotate Scanner' }));
    const confirm = await screen.findByRole('dialog', { name: 'Replace this key?' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Replace key' }));

    const tokenDialog = await screen.findByRole('dialog', { name: 'Key replaced' });
    const rotated = backend.state.keys.get('key_scan')!.token;
    expect(rotated).toMatch(/^fbk_live_rotated_/);
    expect(within(tokenDialog).getByTestId('secret-value').textContent).toBe(rotated);
    expect(within(tokenDialog).getByText(/no longer works/)).toBeTruthy();
    const rotate = backend.requestsTo('POST', '/access/keys/key_scan/rotate');
    expect(rotate[0].body).toEqual({ version: 2, expected_policy_version: 7 });

    fireEvent.click(within(tokenDialog).getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Key replaced' })).toBeNull());
    expect(screen.queryByText(rotated)).toBeNull();

    fireEvent.click(await screen.findByRole('button', { name: 'Create key' }));
    const form = await screen.findByRole('dialog', { name: 'Create key' });
    expect((within(form).getByLabelText('Name') as HTMLInputElement).value).toBe('');
    expect(within(form).queryByTestId('secret-value')).toBeNull();
  });

  it('creates a key for a chosen owner with a permission ceiling and shows the token once', async () => {
    const { client, me } = await signedInClient();
    render(<ApiKeys client={client} me={me} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Create key' }));
    const form = await screen.findByRole('dialog', { name: 'Create key' });
    fireEvent.change(within(form).getByLabelText('Belongs to'), { target: { value: 'p_scanner' } });
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'Lobby scanner' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Create key' }));

    const tokenDialog = await screen.findByRole('dialog', { name: 'Key created' });
    expect(within(tokenDialog).getByTestId('secret-value').textContent).toMatch(/^fbk_live_new_/);
    const create = backend.requestsTo('POST', '/access/keys')[0];
    expect(create.body).toEqual({
      principal: { id: 'p_scanner', version: 3 },
      name: 'Lobby scanner',
      note: '',
      expires_at: null,
      ceiling: [
        { permission: 'fax:send', resource_id: 'res_installation' },
        { permission: 'fax:read', resource_id: 'res_installation' },
      ],
      expected_policy_version: 7,
    });
  });

  it('lists revoked keys with their status and no actions', async () => {
    const { client, me } = await signedInClient();
    render(<ApiKeys client={client} me={me} />);
    const row = (await screen.findByText('Old scanner')).closest('tr')!;
    expect(within(row).getByText('Revoked')).toBeTruthy();
    expect(within(row).queryByRole('button')).toBeNull();
  });

  it('shows a key used seconds ago as just now and an unused key as never', async () => {
    backend.state.keys.get('key_scan')!.last_used_at = toServerTime(new Date(Date.now() - 20_000));
    const { client, me } = await signedInClient();
    render(<ApiKeys client={client} me={me} />);
    const used = (await screen.findByText('Scanner')).closest('tr')!;
    expect(within(used).getByText('just now')).toBeTruthy();
    const unused = screen.getByText('Old scanner').closest('tr')!;
    expect(within(unused).getAllByText('Never')).toHaveLength(2); // never expires, never used
  });

  it('formats older uses as a local time', () => {
    const now = Date.parse('2026-10-03T12:00:00Z');
    expect(lastUsedText(null, now)).toBe('Never');
    expect(lastUsedText('2026-10-03T11:59:10', now)).toBe('just now');
    expect(lastUsedText('2026-10-03T09:00:00', now)).toBe(formatServerTime('2026-10-03T09:00:00'));
    expect(lastUsedText('2026-10-03T09:00:00', now)).not.toMatch(/T09:00/);
  });

  it('edits a migrated key that has no name or note', async () => {
    const { client, me } = await signedInClient();
    render(<ApiKeys client={client} me={me} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Unnamed key' }));
    const form = await screen.findByRole('dialog', { name: 'Edit key' });
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'Front scanner' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Front scanner')).toBeTruthy();
    expect(backend.requestsTo('PATCH', '/access/keys/key_legacy')[0].body).toEqual({
      name: 'Front scanner', version: 1, expected_policy_version: 7,
    });
    expect(await screen.findByRole('button', { name: 'Approve Front scanner' })).toBeTruthy();
  });
});
