import { describe, expect, it } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Logs from '../components/Logs';
import MCP from '../components/MCP';
import { server } from '../test/server';

const keyClient = () => new AdminAPIClient({ kind: 'key', key: 'fbk_live_scan_original' });

const meta = (pendingFields: string[]) => ({
  active_revision_id: 'active', desired_revision_id: pendingFields.length ? 'desired' : 'active', generation: 3,
  apply_state: pendingFields.length ? 'pending_restart' : 'applied', pending_fields: pendingFields,
});

const GENERAL_PROMPT = 'If audit logging is off, enable it to record new events.';

function emptyLogs() {
  server.use(http.get('/admin/logs', () => HttpResponse.json({ items: [], count: 0 })));
}

describe('Logs audit logging status', () => {
  it('says audit logging turns on at restart when that change is saved but not applied', async () => {
    emptyLogs();
    server.use(http.get('/admin/settings', () => HttpResponse.json({
      security: { audit_enabled: true }, _meta: meta(['audit_log_enabled', 'fax_header']),
    })));
    render(<Logs client={keyClient()} />);

    expect(await screen.findByText('Audit logging turns on when Faxbot restarts.')).toBeTruthy();
    expect(screen.queryByText(GENERAL_PROMPT)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Enable Now' })).toBeNull();
  });

  it('says audit logging turns off at restart when that change is saved but not applied', async () => {
    emptyLogs();
    server.use(http.get('/admin/settings', () => HttpResponse.json({
      security: { audit_enabled: false }, _meta: meta(['audit_log_enabled']),
    })));
    render(<Logs client={keyClient()} />);

    expect(await screen.findByText('Audit logging turns off when Faxbot restarts.')).toBeTruthy();
    expect(screen.queryByText(GENERAL_PROMPT)).toBeNull();
  });

  it('keeps the general prompt when other settings are waiting for a restart', async () => {
    emptyLogs();
    let loaded = false;
    server.use(http.get('/admin/settings', () => {
      loaded = true;
      return HttpResponse.json({ security: { audit_enabled: false }, _meta: meta(['fax_header']) });
    }));
    render(<Logs client={keyClient()} />);

    await waitFor(() => expect(loaded).toBe(true));
    expect(await screen.findByText(GENERAL_PROMPT)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Enable Now' })).toBeTruthy();
    expect(screen.queryByText(/when Faxbot restarts/)).toBeNull();
  });

  it('quietly keeps the general prompt for people who cannot read settings', async () => {
    emptyLogs();
    let refused = false;
    server.use(http.get('/admin/settings', () => {
      refused = true;
      return HttpResponse.json({ detail: 'Permission denied.' }, { status: 403 });
    }));
    render(<Logs client={keyClient()} />);

    await waitFor(() => expect(refused).toBe(true));
    expect(await screen.findByText(GENERAL_PROMPT)).toBeTruthy();
    expect(screen.queryByRole('alert', { name: /error/i })).toBeNull();
    expect(screen.queryByText(/couldn't|could not/i)).toBeNull();
  });
});

describe('Provider fax ID refusals', () => {
  const confirmation = { expected_version: 2, provider_sid: 'abc123', confirm_original_account: true as const };

  async function refusal(status: number, detail: string): Promise<string> {
    server.use(http.post('/admin/fax-jobs/:id/reconcile', () => HttpResponse.json({ detail }, { status })));
    try {
      await keyClient().attachProviderIdentity('job-1', confirmation);
    } catch (error) {
      return (error as Error).message;
    }
    throw new Error('attachment unexpectedly succeeded');
  }

  it.each([
    'This fax is not waiting for confirmation, so it does not need a provider fax ID.',
    'This fax was sent by an older Faxbot version; check its status in your provider account.',
    'This fax was not sent through a provider, so there is no fax ID to attach.',
    'Faxbot has no record of sending this fax, so there is no fax ID to attach.',
    'This fax already has a final result, so there is no fax ID to attach.',
    'The provider account that sent this fax is no longer set up; check the fax in that account.',
    'This fax already has a provider fax ID; use Refresh to update its status.',
    'This provider cannot look up fax status; check the fax in your provider account.',
  ])('shows the server sentence "%s"', async (detail) => {
    expect(await refusal(409, detail)).toBe(detail);
  });

  it('maps the remaining fixed refusals to plain sentences', async () => {
    expect(await refusal(409, 'Delivery changed; reload before attaching a provider identity.'))
      .toBe('This fax changed. Reload the job and try again.');
    expect(await refusal(409, 'Delivery record is unavailable.')).toBe('This fax is no longer available. Reload the job.');
    expect(await refusal(409, 'This provider identity already belongs to another delivery from the original account.'))
      .toBe('This fax ID already belongs to another fax from the same provider account.');
    expect(await refusal(400, 'Invalid provider identity reconciliation input.'))
      .toBe('Enter the fax ID exactly as your provider shows it.');
  });

  it('does not show unknown server text', async () => {
    const message = await refusal(409, 'Unexpected provider payload 12345');
    expect(message).not.toContain('12345');
    expect(message).toContain('HTTP 409');
  });
});

describe('MCP health check', () => {
  const mcpSettings = {
    mcp: { sse_enabled: false, http_enabled: true, require_oauth: false, sse_path: '/mcp/sse', http_path: '/mcp/http',
      oauth: { issuer: '', audience: '', jwks_url: '' } },
    _meta: meta([]),
  };

  function mcpServer(health: () => Response) {
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(mcpSettings)),
      http.get('/admin/config', () => HttpResponse.json({ mcp: mcpSettings.mcp })),
      http.get('/mcp/http/health', health),
    );
  }

  it('reports a responding server only for the MCP server reply', async () => {
    mcpServer(() => HttpResponse.json({ status: 'ok', transport: 'streamable-http', server: 'faxbot-mcp', version: '3.0.0' }));
    render(<MCP client={keyClient()} />);
    expect(await screen.findByText('MCP server responding')).toBeTruthy();
  });

  it('does not count a page fallback as a healthy MCP server', async () => {
    mcpServer(() => new HttpResponse('<!doctype html><title>Faxbot</title>', { headers: { 'Content-Type': 'text/html' } }));
    await expect(keyClient().getMcpHealth('/mcp/http/health')).rejects.toThrow();
    render(<MCP client={keyClient()} />);
    expect(await screen.findByText('Health not confirmed')).toBeTruthy();
    expect(screen.queryByText('MCP server responding')).toBeNull();
  });

  it('does not count an unrelated JSON reply as a healthy MCP server', async () => {
    mcpServer(() => HttpResponse.json({ status: 'ok' }));
    await expect(keyClient().getMcpHealth('/mcp/http/health')).rejects.toThrow();
  });
});
