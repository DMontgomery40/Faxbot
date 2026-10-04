import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import RestartNotice from '../components/common/RestartFaxbot';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

describe('Restart now', () => {
  it('restarts Faxbot, waits until it answers again and reloads the screen', async () => {
    const answers = [false, false, true];
    let restarts = 0;
    server.use(
      http.post('/admin/restart', () => { restarts += 1; return HttpResponse.json({ ok: true }); }),
      http.get('/health', () => (answers.shift() ?? true)
        ? HttpResponse.json({ status: 'ok' }) : HttpResponse.error()),
    );
    const onBack = vi.fn();
    render(<RestartNotice client={client()} text="Restart Faxbot to use the new providers." onBack={onBack} pollMs={5} />);
    expect(screen.getByText('Restart Faxbot to use the new providers.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Restart now' }));
    expect(await screen.findByText('Faxbot restarted and is using the saved settings.')).toBeTruthy();
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(restarts).toBe(1);
    expect(screen.queryByRole('button', { name: 'Restart now' })).toBeNull();
  });

  it('says how to restart by hand when the console may not restart this installation', async () => {
    server.use(http.post('/admin/restart', () => HttpResponse.json({ detail: 'Restart not allowed' }, { status: 403 })));
    render(<RestartNotice client={client()} text="Restart Faxbot to use the new providers." pollMs={5} />);
    fireEvent.click(screen.getByRole('button', { name: 'Restart now' }));
    expect(await screen.findByText('Restarting from the console is turned off here. Run docker compose restart api on the server.'))
      .toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Restart now' })).toBeNull();
  });

  it('offers no button to people who may not restart the server', () => {
    render(<RestartNotice client={client()} text="Restart Faxbot to use the new providers." canRestart={false} />);
    expect(screen.getByTestId('restart-notice').textContent)
      .toBe('Restart Faxbot to use the new providers. Run docker compose restart api on the server.');
    expect(screen.queryByRole('button', { name: 'Restart now' })).toBeNull();
  });

  it('says so when Faxbot does not come back', async () => {
    server.use(
      http.post('/admin/restart', () => HttpResponse.json({ ok: true })),
      http.get('/health', () => HttpResponse.error()),
    );
    render(<RestartNotice client={client()} text="Restart Faxbot to use the new providers." pollMs={5} timeoutMs={40} />);
    fireEvent.click(screen.getByRole('button', { name: 'Restart now' }));
    expect(await screen.findByText('Faxbot has not come back yet. Check the server, then reload this page.')).toBeTruthy();
  });
});
