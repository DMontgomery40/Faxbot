import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import {
  CoverInHeaderChoice, HeaderNoticeSettings, RecipientCoverSwitch, SentHeaderNotice,
} from '../components/HeaderNotice';
import { AdminAPIError } from '../api/client';

const NOTICE = 'Confidential: for the addressee only.';

// The header-notice routes in memory, recording each request.
function fakeClient() {
  const requests: Array<{ method: string; path: string; body?: unknown }> = [];
  const state = { organization: null as null | { notice: string }, mailboxes: [] as Array<{ mailbox_id: string; mailbox: string; notice: string }>,
    needs: false };
  const settings = () => ({
    organization: state.organization && { ...state.organization, actor_name: 'Ada Admin', changed_at: '2026-10-10T09:00:00' },
    mailboxes: state.mailboxes.map((item) => ({ ...item, actor_name: null, changed_at: null })), max_length: 120 });
  const client = {
    async call<T>(request: { method: string; path: string; body?: unknown }): Promise<T> {
      requests.push(request);
      const body = (request.body ?? {}) as Record<string, any>;
      if (request.path === '/header-notice' && request.method === 'GET') return settings() as T;
      if (request.path === '/header-notice' && request.method === 'PUT') {
        state.organization = body.notice ? { notice: body.notice } : null;
        return { ...settings(), sentence: body.notice ? 'Saved. Every page of your faxes carries this notice from now on.'
          : 'Removed. New faxes carry no header notice of their own.' } as T;
      }
      if (request.path.startsWith('/header-notice/mailboxes/')) {
        const id = decodeURIComponent(request.path.split('/')[3]);
        state.mailboxes = state.mailboxes.filter((item) => item.mailbox_id !== id);
        if (body.notice) state.mailboxes.push({ mailbox_id: id, mailbox: 'Billing', notice: body.notice });
        return { ...settings(), sentence: 'Saved. Every page of this mailbox’s faxes carries this notice from now on.' } as T;
      }
      if (request.path.startsWith('/header-notice/for-send')) {
        return { notice: request.path.includes('mailbox=m-quiet') ? null : state.organization?.notice ?? null } as T;
      }
      if (request.path.startsWith('/header-notice/recipients/')) {
        if (request.method === 'PUT') state.needs = body.needs_cover;
        return { needs_cover: state.needs, sentence: state.needs ? 'Saved. Faxes to this number always keep their cover sheet.'
          : 'Saved. Senders may send a cover sheet’s notice in the header to this number.' } as T;
      }
      if (request.path.startsWith('/header-notice/faxes/')) {
        return { notice: NOTICE, cover: 'dropped', sentence: 'The first page was a cover sheet. Its notice went in the header of every page instead, so it was not sent: 2 pages went instead of 3.' } as T;
      }
      throw new AdminAPIError(404, 'Not Found', 'Not Found');
    },
  };
  return { client, requests, state };
}

describe('the header notice', () => {
  it('saves the organization notice and a mailbox notice on Sender identity', async () => {
    const fake = fakeClient();
    render(<HeaderNoticeSettings client={fake.client} canWrite mailboxes={[{ id: 'm-billing', label: 'Billing' }]} />);
    const region = await screen.findByRole('region', { name: 'Header notice' });
    fireEvent.change(within(region).getByLabelText('Notice on every page'), { target: { value: NOTICE } });
    fireEvent.click(within(region).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Saved. Every page of your faxes carries this notice from now on.')).toBeTruthy();
    expect(fake.requests.filter((item) => item.method === 'PUT')).toEqual([
      { method: 'PUT', path: '/header-notice', body: { notice: NOTICE } }]);
    fireEvent.mouseDown(within(region).getByRole('combobox', { name: 'Mailbox' }));
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'Billing' }));
    fireEvent.change(within(region).getByLabelText('Its notice'), { target: { value: 'Billing office: confidential.' } });
    fireEvent.click(within(region).getByRole('button', { name: 'Add' }));
    expect(await screen.findByText('Billing: Billing office: confidential.')).toBeTruthy();
  });

  it('is read-only without settings changes and hidden without settings access', async () => {
    const fake = fakeClient();
    fake.state.organization = { notice: NOTICE };
    render(<HeaderNoticeSettings client={fake.client} canWrite={false} mailboxes={[]} />);
    const field = await screen.findByLabelText('Notice on every page');
    await waitFor(() => expect((field as HTMLInputElement).value).toBe(NOTICE));
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    const forbidden = { call: async () => { throw new AdminAPIError(403, 'Forbidden', 'Forbidden'); } };
    const { container } = render(<div data-testid="hidden"><HeaderNoticeSettings client={forbidden as never} canWrite mailboxes={[]} /></div>);
    await waitFor(() => expect(within(container).getByTestId('hidden').textContent).toBe(''));
  });

  it('offers the cover choice on Send a fax only when a notice applies', async () => {
    const fake = fakeClient();
    let checked = false;
    const { rerender } = render(<CoverInHeaderChoice client={fake.client} mailbox={null} checked={checked}
      onChange={(value) => { checked = value; }} />);
    await waitFor(() => expect(fake.requests.length).toBe(1));
    expect(screen.queryByTestId('send-cover-in-header')).toBeNull();
    fake.state.organization = { notice: NOTICE };
    rerender(<CoverInHeaderChoice client={fake.client} mailbox="m-billing" checked={checked} onChange={(value) => { checked = value; }} />);
    fireEvent.click(await screen.findByRole('checkbox'));
    expect(checked).toBe(true);
    expect(screen.getByText(`Every page carries: ${NOTICE}`)).toBeTruthy();
  });

  it('switches a recipient that needs a cover sheet, and shows what Sent details say', async () => {
    const fake = fakeClient();
    render(<RecipientCoverSwitch client={fake.client} number="+13035550150" canWrite />);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Needs a cover sheet' }));
    expect(await screen.findByText('Saved. Faxes to this number always keep their cover sheet.')).toBeTruthy();
    expect(fake.requests[fake.requests.length - 1]).toEqual({ method: 'PUT', path: '/header-notice/recipients/%2B13035550150', body: { needs_cover: true } });
    render(<SentHeaderNotice client={fake.client} jobId="job-1" />);
    expect(await screen.findByTestId('sent-header-notice')).toBeTruthy();
    expect(screen.getByText(/2 pages went instead of 3/)).toBeTruthy();
  });
});
