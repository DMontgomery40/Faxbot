import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Diagnostics from '../components/Diagnostics';
import { formatServerTime } from '../api/time';
import { server } from '../test/server';
import { settingsFixture } from '../test/settingsFixture';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const result = {
  timestamp: '2026-10-03T12:34:56.789000',
  backend: 'phaxio', default_backend: 'phaxio', outbound_backend: 'phaxio', inbound_backend: 'sip',
  configuration: { active_revision_id: '7f1c2a9e-0000-4000-8000-00000000abcd', desired_revision_id: '9e2b3c1d-0000-4000-8000-00000000ef01',
    generation: 42, pending_restart: true },
  checks: {
    outbound: { backend_config: true, sending_disabled: false, requires_ami: false },
    security: { enforce_https: true, pdf_token_ttl: 60 },
    storage: { type: 'local', bucket: null },
  },
  check_outcomes: { outbound: { backend_config: 'pass' }, security: { enforce_https: 'pass' }, storage: {} },
  summary: { healthy: true, critical_issues: [], warnings: [
    'Desired settings are pending a full installation restart; diagnostics describe the active revision.',
  ] },
};

describe('Diagnostics', () => {
  it('offers to restart Faxbot, in those words', () => {
    render(<Diagnostics client={client()} />);
    expect(screen.getByRole('button', { name: 'Restart Faxbot' })).toBeTruthy();
    expect(screen.queryByText(/Restart API/)).toBeNull();
  });

  it('shows configuration as plain facts, with no revision identifiers or counters', async () => {
    server.use(http.post('/admin/diagnostics/run', () => HttpResponse.json(result)));
    render(<Diagnostics client={client()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Run Diagnostics' }));
    expect(await screen.findByText('Some saved settings take effect after Faxbot restarts; these results describe the settings in use now.')).toBeTruthy();
    expect(screen.getByText(/Saved changes waiting for a restart:/).textContent).toContain('Yes');
    expect(screen.getByText(`Checked at ${formatServerTime(result.timestamp)}`)).toBeTruthy();
    expect(screen.getAllByText('Yes').length).toBeGreaterThan(0);
    expect(screen.getAllByText('No').length).toBeGreaterThan(0);
    expect(screen.getByText('Not set')).toBeTruthy();
    const page = document.body.textContent ?? '';
    for (const hidden of ['7f1c2a9e', '9e2b3c1d', 'revision', 'generation', 'Desired settings', '2026-10-03T12:34']) {
      expect(page).not.toContain(hidden);
    }
  });

  it('copies diagnostics without revision identifiers or counters', async () => {
    server.use(http.post('/admin/diagnostics/run', () => HttpResponse.json(result)));
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<Diagnostics client={client()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Run Diagnostics' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Copy results' }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    const copied = JSON.parse(writeText.mock.calls[0][0]);
    expect(copied.configuration).toEqual({ pending_restart: true });
    expect(JSON.stringify(copied)).not.toMatch(/revision|generation|7f1c2a9e/);
  });
});

describe('Diagnostics check names', () => {
  it('names each check in plain words, never by its code', async () => {
    server.use(http.post('/admin/diagnostics/run', () => HttpResponse.json({ ...result, checks: {
      ...result.checks, inbound: { ami_password_not_default: true, asterisk_secret_set: true, requires_ami: true } },
    check_outcomes: { ...result.check_outcomes, inbound: { ami_password_not_default: 'pass', asterisk_secret_set: 'pass' } } })));
    render(<Diagnostics client={client()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Run Diagnostics' }));
    expect(await screen.findByText('Fax engine password changed from the default')).toBeTruthy();
    expect(screen.getByText('Document links for fax services last (minutes)')).toBeTruthy();
    expect(screen.getAllByText('Needs the fax engine').length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toMatch(/\bAmi\b|Pdf Token Ttl|Requires Ami/);
  });
});

describe('Reading saved settings again', () => {
  it('asks the server to read its saved settings again and says pending changes still wait', async () => {
    let asked = 0;
    server.use(http.post('/admin/settings/reload', () => { asked += 1; return HttpResponse.json(settingsFixture()); }));
    render(<Diagnostics client={client()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Read saved settings again' }));
    expect(await screen.findByText('Faxbot read its saved settings again. Changes waiting for a restart still wait.')).toBeTruthy();
    expect(asked).toBe(1);
  });
});
