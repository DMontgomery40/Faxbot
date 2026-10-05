import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import PairPhoneDialog from '../components/access/PairPhoneDialog';
import { toServerTime } from '../api/time';
import { backend, server } from '../test/server';

describe('phone pairing', () => {
  it('shows the six-digit code large with a QR code and a countdown', async () => {
    const expires = toServerTime(new Date(Date.now() + 5 * 60 * 1000));
    server.use(http.post('/admin/tunnel/pair', () => HttpResponse.json({ code: '482913', expires_at: expires })));
    await AdminAPIClient.login('admin', 'correct horse');
    const client = new AdminAPIClient({ kind: 'session', csrf: null });
    await client.me();
    render(<PairPhoneDialog client={client} open onClose={() => undefined} />);

    const dialog = await screen.findByRole('dialog', { name: 'Pair a phone' });
    expect((await within(dialog).findByTestId('pairing-code')).textContent).toBe('482913');
    expect(within(dialog).getByRole('img', { name: 'Pairing code 482913' })).toBeTruthy();
    expect(within(dialog).getByText(/^Expires in [45]:\d\d$/)).toBeTruthy();
    expect(backend.state.requests.length).toBeGreaterThan(0);
  });
});
