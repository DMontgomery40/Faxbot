import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Settings, { SINCH_PASSWORD_NEEDED, sinchBasicProblem } from '../components/Settings';
import { server } from '../test/server';
import { settingsFixture, withDirections } from '../test/settingsFixture';

describe("Sinch's user name and password for received faxes", () => {
  it('refuses to save the user name without a password, with one sentence', () => {
    expect(sinchBasicProblem({ sinch_inbound_basic_user: 'synthetic-sinch-webhook', sinch_inbound_basic_pass: '' },
      ['sinch_inbound_basic_user'])).toBe(SINCH_PASSWORD_NEEDED);
    expect(SINCH_PASSWORD_NEEDED).toBe('Enter the password Sinch sends as well; Faxbot does not accept the user name without it.');
  });

  it('saves both together, a stored password, clearing both, or changes elsewhere', () => {
    expect(sinchBasicProblem({ sinch_inbound_basic_user: 'synthetic-sinch-webhook',
      sinch_inbound_basic_pass: 'synthetic-webhook-pass' }, ['sinch_inbound_basic_user', 'sinch_inbound_basic_pass'])).toBeNull();
    // A password already saved shows masked and still counts.
    expect(sinchBasicProblem({ sinch_inbound_basic_user: 'synthetic-sinch-webhook', sinch_inbound_basic_pass: '****pass' },
      ['sinch_inbound_basic_user'])).toBeNull();
    expect(sinchBasicProblem({ sinch_inbound_basic_user: '', sinch_inbound_basic_pass: '' },
      ['sinch_inbound_basic_user', 'sinch_inbound_basic_pass'])).toBeNull();
    expect(sinchBasicProblem({ sinch_inbound_basic_user: 'synthetic-sinch-webhook', sinch_inbound_basic_pass: '' },
      ['max_file_size_mb'])).toBeNull();
  });
});

describe('The Sinch page gives the exact Incoming webhook URL', () => {
  const keyClient = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

  function sinchServer(data: Record<string, any>, writes: Array<Record<string, unknown>>) {
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(data)),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ok: true, changed: true, _meta: { ...data._meta, desired_revision_id: 'rev-b' } });
      }),
    );
  }

  it('shows the address to paste and where it goes in Sinch, and saves another address for Sinch to use', async () => {
    const writes: Array<Record<string, unknown>> = [];
    const data = settingsFixture((value) => {
      withDirections(value, 'sinch', 'sinch');
      value.sinch = { project_id: 'synthetic-project', base_url: '', api_key: '***', api_secret: '***', configured: true,
        webhook_base_url: '', incoming_webhook_url: 'https://fax.example/sinch-inbound', incoming_webhook_login_url: null };
    });
    sinchServer(data, writes);
    render(<Settings client={keyClient()} sections={['sinch']} title="Sinch" canWrite />);
    const box = await screen.findByTestId('sinch-incoming-webhook');
    expect(within(box).getByText('https://fax.example/sinch-inbound')).toBeTruthy();
    expect(within(box).getByText(/open Fax, then Services, click Edit beside your fax service/)).toBeTruthy();
    const address = screen.getByLabelText('Address Sinch sends received faxes to (optional)') as HTMLInputElement;
    fireEvent.change(address, { target: { value: 'https://fax-hooks.example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', sinch_webhook_base_url: 'https://fax-hooks.example.com' });
  });

  it('with a user name and password, shows where the password goes in the address and never the password', async () => {
    const data = settingsFixture((value) => {
      withDirections(value, 'sinch', 'sinch');
      value.sinch = { project_id: 'synthetic-project', base_url: '', api_key: '***', api_secret: '***', configured: true,
        webhook_base_url: 'https://fax-hooks.example.com', incoming_webhook_url: 'https://fax-hooks.example.com/sinch-inbound',
        incoming_webhook_login_url: 'https://synthetic-hooks:PASSWORD@fax-hooks.example.com/sinch-inbound' };
      value.inbound.sinch = { basic_auth_configured: true, basic_user: 'synthetic-hooks', basic_pass: '***' };
    });
    sinchServer(data, []);
    render(<Settings client={keyClient()} sections={['sinch']} title="Sinch" canWrite />);
    const box = await screen.findByTestId('sinch-incoming-webhook');
    expect(within(box).getByText('https://synthetic-hooks:PASSWORD@fax-hooks.example.com/sinch-inbound')).toBeTruthy();
    expect(within(box).getByText(/with your password in place of PASSWORD/)).toBeTruthy();
  });
});
