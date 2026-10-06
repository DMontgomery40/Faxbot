import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Diagnostics, { reportAsText } from '../components/Diagnostics';
import type { DiagnosticsReport } from '../api/types';
import { server } from '../test/server';
import { settingsFixture } from '../test/settingsFixture';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const report: DiagnosticsReport = {
  checked_at: '2026-10-04T21:00:00Z',
  checked_at_text: '4 Oct 3:00 PM MDT',
  status: 'problem',
  summary: '1 problem keeps Faxbot from working fully, and 1 more thing needs attention.',
  sections: [
    { id: 'sending', title: 'Sending', checks: [
      { id: 'sending.provider', section: 'sending', title: 'Sending account', status: 'problem',
        sentence: 'HumbleFax did not accept Faxbot\'s sign-in details. Enter them again in Providers.',
        fix: { label: 'Open Providers', page: 'providers/sending' } },
      { id: 'sending.recent', section: 'sending', title: 'Recent faxes', status: 'ok',
        sentence: 'Last fax delivered 4 Oct 2:55 PM MDT. In the last 7 days: 6 delivered, 1 failed.', fix: null },
    ] },
    { id: 'engine', title: 'Fax engine', checks: [
      { id: 'engine.t38', section: 'engine', title: 'Fax over IP (T.38)', status: 'attention',
        sentence: 'Off: your network changes port numbers.', fix: { label: 'Open carrier trunk', page: 'providers/trunk' } },
      { id: 'engine.running', section: 'engine', title: 'Fax engine', status: 'off',
        sentence: 'Your providers send and receive faxes themselves.', fix: null },
    ] },
  ],
};
const empty = { checked_at: null, checked_at_text: '', status: null, summary: null, sections: [] };

describe('Diagnostics', () => {
  it('shows the last results at once, without running the checks again', async () => {
    let runs = 0;
    server.use(http.get('/admin/diagnostics/report', () => HttpResponse.json(report)),
      http.post('/admin/diagnostics/report', () => { runs += 1; return HttpResponse.json(report); }));
    render(<Diagnostics client={client()} />);
    expect(await screen.findByText(report.summary!)).toBeTruthy();
    expect(screen.getByText(/Last checked 4 Oct 3:00 PM MDT\./)).toBeTruthy();
    expect(screen.getByText('Sending account')).toBeTruthy();
    expect(screen.getByText('Off: your network changes port numbers.')).toBeTruthy();
    expect(runs).toBe(0);
  });

  it('checks by itself on the first visit, and again on Check now', async () => {
    let runs = 0;
    server.use(http.get('/admin/diagnostics/report', () => HttpResponse.json(empty)),
      http.post('/admin/diagnostics/report', () => { runs += 1; return HttpResponse.json(report); }));
    render(<Diagnostics client={client()} />);
    expect(await screen.findByText(report.summary!)).toBeTruthy();
    expect(runs).toBe(1);
    fireEvent.click(screen.getByRole('button', { name: 'Check now' }));
    await waitFor(() => expect(runs).toBe(2));
  });

  it('opens the page that fixes a problem', async () => {
    server.use(http.get('/admin/diagnostics/report', () => HttpResponse.json(report)));
    const onNavigate = vi.fn();
    render(<Diagnostics client={client()} onNavigate={onNavigate} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Open Providers' }));
    expect(onNavigate).toHaveBeenCalledWith('providers/sending');
    fireEvent.click(screen.getByRole('button', { name: 'Open carrier trunk' }));
    expect(onNavigate).toHaveBeenLastCalledWith('providers/trunk');
  });

  it('copies the same sentences a person reads, with no codes', async () => {
    server.use(http.get('/admin/diagnostics/report', () => HttpResponse.json(report)));
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<Diagnostics client={client()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Copy results' }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toBe(reportAsText(report));
    expect(copied).toContain('- Sending account: Not working. HumbleFax did not accept');
    expect(copied).toContain('- Fax engine: Not in use.');
    expect(copied).not.toMatch(/sending\.provider|providers\/sending|"status"/);
  });

  it('offers to restart Faxbot, in those words', async () => {
    server.use(http.get('/admin/diagnostics/report', () => HttpResponse.json(report)));
    render(<Diagnostics client={client()} />);
    expect(screen.getByRole('button', { name: 'Restart Faxbot' })).toBeTruthy();
    expect(screen.queryByText(/Restart API/)).toBeNull();
    await screen.findByText(report.summary!);
  });

  it('says so when the checks cannot finish', async () => {
    server.use(http.get('/admin/diagnostics/report', () => HttpResponse.json(empty)),
      http.post('/admin/diagnostics/report', () => HttpResponse.json({ detail: 'x' }, { status: 500 })));
    render(<Diagnostics client={client()} />);
    expect(await screen.findByText('Faxbot could not finish the checks. Try again in a moment.')).toBeTruthy();
  });
});

describe('Reading saved settings again', () => {
  it('asks the server to read its saved settings again and says pending changes still wait', async () => {
    let asked = 0;
    server.use(http.get('/admin/diagnostics/report', () => HttpResponse.json(report)),
      http.post('/admin/settings/reload', () => { asked += 1; return HttpResponse.json(settingsFixture()); }));
    render(<Diagnostics client={client()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Read saved settings again' }));
    expect(await screen.findByText('Faxbot read its saved settings again. Changes waiting for a restart still wait.')).toBeTruthy();
    expect(asked).toBe(1);
  });
});
