import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Settings from '../components/Settings';
import { settingsFixture } from '../test/settingsFixture';
import { server } from '../test/server';

describe('generic settings write permission', () => {
  it.each([undefined, false])('keeps fields and save actions read-only for canWrite=%s', async (canWrite) => {
    const update = vi.fn();
    const persist = vi.fn();
    const client = new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
    server.use(http.get('/admin/settings', () => HttpResponse.json(settingsFixture())));
    vi.spyOn(client, 'updateSettings').mockImplementation(update);
    vi.spyOn(client, 'persistSettings').mockImplementation(persist);
    const { container } = render(<Settings client={client} canWrite={canWrite}
      sections={['mcp', 'security', 'advanced', 'backup', 'diagnostics']} />);

    expect(await screen.findByText('Read only: you do not have permission to change settings.')).toBeTruthy();
    const fieldset = container.querySelector('fieldset')!;
    await waitFor(() => expect(fieldset.querySelectorAll('input').length).toBeGreaterThan(0));
    for (const control of fieldset.querySelectorAll('input, select, button')) {
      expect(control.matches(':disabled')).toBe(true);
    }
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save a recovery copy' }));
    expect(update).not.toHaveBeenCalled();
    expect(persist).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: 'Reload' }) as HTMLButtonElement).disabled).toBe(false);
  });
});
