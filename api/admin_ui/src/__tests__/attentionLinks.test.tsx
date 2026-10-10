// The lists Needs attention opens keep their filter in the page address, so a link, a reload and Back
// all show the same filtered list: Sent by status and held faxes, Received by show=, Expected by show=.
import { describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import App from '../App';
import { backend, server } from '../test/server';

async function signInAt(address: string) {
  window.history.replaceState(null, '', `/${address}`);
  render(<App />);
  await screen.findByRole('heading', { name: 'Sign in' });
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct horse' } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
  await screen.findByText('Ada Admin');
}

// Back, or a link opened in the same tab: the address changes and the shell follows it.
function goTo(address: string) {
  act(() => {
    window.history.replaceState(null, '', address);
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  });
}

// Expected is a page for people who read the work queue.
function grantWorkRead() {
  const admin = backend.state.principals.get('p_admin')!;
  admin.permissions = [...new Set([...admin.permissions, 'work:read'])];
}

const statusShown = () => document.getElementById('jobs-status-filter')?.textContent;

describe('Sent keeps its filter in the address', () => {
  it('lists the status the address names, writes a new choice there, and follows Back', async () => {
    const asked: string[] = [];
    server.use(http.get('/admin/fax-jobs', ({ request }) => {
      asked.push(new URL(request.url).searchParams.get('status') ?? '');
      return HttpResponse.json({ total: 0, jobs: [] });
    }));
    await signInAt('#/faxes/sent?status=failed');
    await screen.findByRole('heading', { name: 'Sent' });
    await waitFor(() => expect(statusShown()).toBe('Failed'));
    expect(asked).toContain('failed');
    expect(asked).not.toContain('');

    fireEvent.mouseDown(screen.getByRole('combobox', { name: /Show/ }));
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'Needs review' }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/sent?status=reconciliation_required'));
    await waitFor(() => expect(asked).toContain('reconciliation_required'));

    goTo('#/faxes/sent?status=failed');
    await waitFor(() => expect(statusShown()).toBe('Failed'));
    fireEvent.mouseDown(screen.getByRole('combobox', { name: /Show/ }));
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'All' }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/sent'));
  });

  it('lists only the last hours the address names, says so, and shows every fax with that status on request', async () => {
    const asked: Array<[string, string]> = [];
    server.use(http.get('/admin/fax-jobs', ({ request }) => {
      const query = new URL(request.url).searchParams;
      asked.push([query.get('status') ?? '', query.get('since_hours') ?? '']);
      return HttpResponse.json({ total: 0, jobs: [] });
    }));
    await signInAt('#/faxes/sent?status=failed&since=24h');
    expect(await screen.findByText('Only faxes whose state changed in the last 24 hours are shown.')).toBeTruthy();
    await waitFor(() => expect(asked).toContainEqual(['failed', '24']));
    expect(asked).not.toContainEqual(['failed', '']);
    fireEvent.click(screen.getByRole('button', { name: 'Show from any time' }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/sent?status=failed'));
    await waitFor(() => expect(asked).toContainEqual(['failed', '']));
    expect(screen.queryByText('Only faxes whose state changed in the last 24 hours are shown.')).toBeNull();
  });

  it('lists every sent fax for a status it does not know', async () => {
    await signInAt('#/faxes/sent?status=nonsense');
    await screen.findByRole('heading', { name: 'Sent' });
    await waitFor(() => expect(statusShown()).toBe('All'));
  });

  it('shows only the faxes your rules are holding, and every sent fax again on request', async () => {
    const hold = { id: 'h-1', job_id: 'f'.repeat(32), kind: 'approval', to_number: '+15550100001', pages: 24,
      sender_name: 'Nia New', requested_at: '2026-10-09T16:00:00', until: null,
      reason: 'Waiting for approval: the rule matched.', can_decide: true, version: 2 };
    server.use(http.get('/routing/holds', () => HttpResponse.json({ holds: [hold], can_approve: true })));
    await signInAt('#/faxes/sent?show=held');
    expect(await screen.findByRole('table', { name: 'Held faxes' })).toBeTruthy();
    expect(screen.queryByRole('combobox', { name: /Show/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show all sent faxes' }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/sent'));
    await waitFor(() => expect(statusShown()).toBe('All'));
  });

  it('says when no fax is held', async () => {
    await signInAt('#/faxes/sent?show=held');
    expect(await screen.findByText('Your rules are not holding any faxes.')).toBeTruthy();
  });
});

// The views Expected was asked for, and an empty report for its What is missing tab.
function expectedServer(asked: string[] = []) {
  server.use(
    http.get('/expected-faxes', ({ request }) => {
      asked.push(new URL(request.url).searchParams.get('view') ?? '');
      return HttpResponse.json({ expected: [] });
    }),
    http.get('/expected-faxes/report', () => HttpResponse.json({ summary: 'Nothing is missing.', overdue: 0, not_stored: 0,
      unmatched_expected: [], unmatched_arrivals: [] })),
  );
  return asked;
}

describe('Expected keeps its view in the address', () => {
  it('opens the Expected tab on the view the address names, writes a new choice there, and follows Back', async () => {
    const asked = expectedServer();
    grantWorkRead();
    await signInAt('#/faxes/expected?show=overdue');
    const views = await screen.findByRole('group', { name: 'Which expected faxes' });
    await waitFor(() => expect(within(views).getByRole('button', { name: 'Overdue' }).getAttribute('aria-pressed')).toBe('true'));
    expect(asked).toContain('overdue');
    expect(asked).not.toContain('waiting');

    fireEvent.click(within(views).getByRole('button', { name: 'To confirm' }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/expected?show=proposed'));
    await waitFor(() => expect(asked).toContain('proposed'));

    // From another tab, a link to a view opens the Expected tab on it.
    fireEvent.click(screen.getByRole('tab', { name: 'What is missing' }));
    goTo('#/faxes/expected?show=overdue');
    const again = await screen.findByRole('group', { name: 'Which expected faxes' });
    await waitFor(() => expect(within(again).getByRole('button', { name: 'Overdue' }).getAttribute('aria-pressed')).toBe('true'));

    fireEvent.click(within(again).getByRole('button', { name: 'Waiting' }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/expected'));
  });

  it('shows the waiting faxes for a view it does not know', async () => {
    expectedServer();
    grantWorkRead();
    await signInAt('#/faxes/expected?show=nonsense');
    const views = await screen.findByRole('group', { name: 'Which expected faxes' });
    await waitFor(() => expect(within(views).getByRole('button', { name: 'Waiting' }).getAttribute('aria-pressed')).toBe('true'));
  });
});

describe('Received keeps its filter in the address', () => {
  it('follows Back to the filter the address names', async () => {
    await signInAt('#/faxes/received?show=not-delivered');
    const filters = await screen.findByRole('group', { name: 'Which faxes' });
    await waitFor(() => expect(within(filters).getByRole('button', { name: /Not delivered by email/ }).getAttribute('aria-pressed')).toBe('true'));
    fireEvent.click(within(filters).getByRole('button', { name: /^All/ }));
    await waitFor(() => expect(window.location.hash).toBe('#/faxes/received'));
    goTo('#/faxes/received?show=not-delivered');
    await waitFor(() => expect(within(filters).getByRole('button', { name: /Not delivered by email/ }).getAttribute('aria-pressed')).toBe('true'));
  });
});
