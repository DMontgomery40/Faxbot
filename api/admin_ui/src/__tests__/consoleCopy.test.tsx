import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Received from '../components/Received';
import ScriptsTests from '../components/ScriptsTests';
import Logs from '../components/Logs';
import MCP from '../components/MCP';
import { server } from '../test/server';

const keyClient = () => new AdminAPIClient({ kind: 'key', key: 'fbk_live_scan_original' });

const meta = (pendingFields: string[]) => ({
  active_revision_id: 'active', desired_revision_id: pendingFields.length ? 'desired' : 'active', generation: 3,
  apply_state: pendingFields.length ? 'pending_restart' : 'applied', pending_fields: pendingFields,
});

const GENERAL_PROMPT = 'If event recording is off, turn it on to record new events.';

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

    expect(await screen.findByText('Event recording turns on when Faxbot restarts.')).toBeTruthy();
    expect(screen.queryByText(GENERAL_PROMPT)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Turn on now' })).toBeNull();
  });

  it('says audit logging turns off at restart when that change is saved but not applied', async () => {
    emptyLogs();
    server.use(http.get('/admin/settings', () => HttpResponse.json({
      security: { audit_enabled: false }, _meta: meta(['audit_log_enabled']),
    })));
    render(<Logs client={keyClient()} />);

    expect(await screen.findByText('Event recording turns off when Faxbot restarts.')).toBeTruthy();
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
    expect(screen.getByRole('button', { name: 'Turn on now' })).toBeTruthy();
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
    // SSE is kept one release; the switch says so.
    expect(screen.getByTestId('sse-retiring').textContent).toBe('SSE goes away in the next release. Use Streamable HTTP.');
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

describe('Inbox for people without provider access', () => {
  function inboxServer() {
    const calls = { callbacks: 0 };
    server.use(
      http.get('/inbound', () => HttpResponse.json([])),
      http.get('/admin/inbound/callbacks', () => {
        calls.callbacks += 1;
        return HttpResponse.json({ detail: 'Permission denied.' }, { status: 403 });
      }),
    );
    return calls;
  }

  it('hides provider setup and test faxes from a fax operator and never shows a raw error', async () => {
    const calls = inboxServer();
    const operator = new Set(['fax:send', 'fax:read', 'fax:document', 'inbound:list', 'inbound:read', 'inbound:document']);
    render(<Received client={keyClient()} inboundEnabled permissions={operator} />);

    expect(await screen.findByText('No received faxes yet.')).toBeTruthy();
    expect(screen.queryByText('Inbound Fax Configuration')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add Test Fax' })).toBeNull();
    expect(screen.queryByText(/API Error|403|Forbidden/)).toBeNull();
    expect(calls.callbacks).toBe(0);
  });

  it('has no setup panel or test fax button even for provider administrators, and stays quiet when receiving details fail', async () => {
    const calls = inboxServer();
    render(<Received client={keyClient()} inboundEnabled permissions={new Set(['inbound:list', 'providers:read', 'providers:write'])} />);
    expect(await screen.findByText('No received faxes yet.')).toBeTruthy();
    await waitFor(() => expect(calls.callbacks).toBe(1));
    expect(screen.queryByText('Inbound Fax Configuration')).toBeNull();
    expect(screen.queryByText(/Requirements|HIPAA compliance/)).toBeNull();
    expect(screen.queryByRole('button', { name: /Test Fax/i })).toBeNull();
    expect(screen.queryByText(/API Error|Forbidden|couldn't be loaded/)).toBeNull();
  });

  it('adds a test fax from System, Developer, Scripts & checks', async () => {
    const added: unknown[] = [];
    server.use(
      http.get('/admin/settings', () => HttpResponse.json({ backend: { type: 'phaxio' }, inbound: { enabled: true } })),
      http.post('/admin/inbound/simulate', async ({ request }) => {
        added.push(await request.json());
        return HttpResponse.json({ id: 'test-1', status: 'received' });
      }),
    );
    render(<ScriptsTests client={keyClient()} onNavigate={() => undefined} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a test fax' }));
    expect(await screen.findByText('A test fax was added to Received.')).toBeTruthy();
    expect(added).toEqual([{ pages: 1, status: 'received' }]);
  });
});
