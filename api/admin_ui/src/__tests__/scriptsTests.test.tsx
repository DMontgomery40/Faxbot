import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import ScriptsTests from '../components/ScriptsTests';
import { server } from '../test/server';
import { settingsFixture } from '../test/settingsFixture';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

function page(callbacks: object, { inbound = true } = {}) {
  const settings = settingsFixture();
  server.use(
    http.get('/admin/settings', () => HttpResponse.json({ ...settings, inbound: { ...settings.inbound, enabled: inbound } })),
    http.get('/admin/inbound/callbacks', () => HttpResponse.json(callbacks)),
  );
  const onNavigate = vi.fn();
  render(<ScriptsTests client={client()} onNavigate={onNavigate} />);
  return onNavigate;
}

describe('Scripts & checks', () => {
  it('is titled like its place in the menu and names the receiving provider, not the sending one', async () => {
    page({ backend: 'sip', callbacks: [], receiving: { ready: true, message: 'Received faxes reach Faxbot by themselves.' } });
    expect(screen.getByRole('heading', { name: 'Scripts & checks' })).toBeTruthy();
    expect(await screen.findByText('Received faxes reach Faxbot by themselves.')).toBeTruthy();
    expect(screen.getByText(/How .* reaches Faxbot/)).toBeTruthy();
    const text = document.body.textContent ?? '';
    for (const gone of ['Phaxio', 'Cloudflared', 'WireGuard', 'Tailscale', 'X-Internal-Secret', 'Scripts & Tests']) {
      expect(text).not.toContain(gone);
    }
  });

  it('adds one test fax and opens Received', async () => {
    let body: any = null;
    server.use(http.post('/admin/inbound/simulate', async ({ request }) => { body = await request.json(); return HttpResponse.json({ id: 'synthetic', status: 'ok' }); }));
    const onNavigate = page({ backend: 'humblefax', callbacks: [] });
    fireEvent.change(await screen.findByLabelText('Your fax number it arrives on (optional)'), { target: { value: '+13035550100' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add a test fax' }));
    expect(await screen.findByText('A test fax was added to Received.')).toBeTruthy();
    expect(body).toMatchObject({ to: '+13035550100', pages: 1 });
    fireEvent.click(screen.getByRole('button', { name: 'Open Received' }));
    expect(onNavigate).toHaveBeenCalledWith('faxes/received');
    expect(screen.getAllByRole('button', { name: 'Add a test fax' })).toHaveLength(1);
  });

  it('says receiving is off instead of offering a test fax', async () => {
    page({ backend: 'humblefax', callbacks: [] }, { inbound: false });
    expect(await screen.findByText('Receiving is turned off, so there is nowhere to add a test fax.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add a test fax' })).toBeNull();
  });

  it('shows the address a provider calls, with a copy button and its guide', async () => {
    page({ backend: 'sinch', callbacks: [{ name: 'Sinch Fax Inbound', url: 'https://fax.example.com/sinch-inbound', notes: 'Set this as the incoming fax webhook in Sinch.' }] });
    expect(await screen.findByText('https://fax.example.com/sinch-inbound')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy Sinch Fax Inbound address' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Sinch setup guide' })).toBeTruthy();
  });

  it('says there is nothing to set when Faxbot collects faxes itself', async () => {
    page({ backend: 'humblefax', callbacks: [] });
    expect(await screen.findByText('Faxbot collects received faxes from HumbleFax itself, so there is no address to give HumbleFax.')).toBeTruthy();
  });

  it('lists what the fax engine reports, in a table', async () => {
    server.use(http.get('/admin/diagnostics/engine/registrations', () => HttpResponse.json({
      view: 'registrations', title: 'Trunk sign-ins', columns: ['Name', 'Status'], rows: [['trunk-registration', 'Registered']],
      available: true, message: null })),
    http.get('/admin/diagnostics/engine/faxes', () => HttpResponse.json({
      view: 'faxes', title: 'Faxes in progress', columns: ['Call'], rows: [], available: true, message: 'No fax is in progress.' })));
    page({ backend: 'sip', callbacks: [], receiving: { ready: true, message: 'Ready.' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Trunk sign-ins' }));
    const table = await screen.findByRole('table', { name: 'Trunk sign-ins' });
    expect(table.textContent).toContain('Registered');
    fireEvent.click(screen.getByRole('button', { name: 'Faxes in progress' }));
    expect(await screen.findByText('No fax is in progress.')).toBeTruthy();
  });

  it('offers no server commands', async () => {
    page({ backend: 'sip', callbacks: [], receiving: { ready: true, message: 'Ready.' } });
    expect(await screen.findByText('Ready.')).toBeTruthy();
    expect(screen.queryByText('Server checks')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Python version' })).toBeNull();
  });
});
