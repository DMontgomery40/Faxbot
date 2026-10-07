import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Settings from '../components/Settings';
import { humblefaxStatusText } from '../components/HumbleFaxReceiving';
import type { HumbleFaxStatus } from '../api/types';
import { server } from '../test/server';
import { receipt, settingsFixture } from '../test/settingsFixture';

type Json = Record<string, any>;

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const ENV_NAME = /[A-Z]{3,}_[A-Z_]{2,}/;

function status(changes: Partial<HumbleFaxStatus> = {}): HumbleFaxStatus {
  return { account: 'humblefax', receiving: true, reason: null, turned_on: true, receiving_provider: false,
    poll_seconds: 60, checked_at: null, found: null, problem: null, ...changes };
}

function backend(data: Json, current: HumbleFaxStatus, checked?: HumbleFaxStatus | { status: number; detail: string }) {
  const writes: Json[] = [];
  const checks: number[] = [];
  server.use(
    http.get('/admin/settings', () => HttpResponse.json(data)),
    http.get('/plugins', () => HttpResponse.json({ items: [] })),
    http.get('/admin/inbound/humblefax', () => HttpResponse.json(current)),
    http.post('/admin/inbound/humblefax/check', () => {
      checks.push(1);
      if (checked && 'detail' in checked) return HttpResponse.json({ detail: checked.detail }, { status: checked.status });
      return HttpResponse.json(checked ?? current);
    }),
    http.put('/admin/settings', async ({ request }) => {
      writes.push(await request.json() as Json);
      data._meta = { ...data._meta, desired_revision_id: 'rev-b', active_revision_id: 'rev-b' };
      return HttpResponse.json(receipt('rev-b'));
    }),
  );
  return { writes, checks };
}

function sendsWithHumbleFax(data: Json) {
  data.backend.type = 'humblefax';
  data.hybrid = { outbound_backend: 'humblefax', inbound_backend: 'sip', outbound_override: '', inbound_override: 'sip' };
  data.inbound.enabled = true;
}

describe('Receiving through HumbleFax in Settings', () => {
  it('turns on receiving beside the trunk, sets how often to check, and saves both', async () => {
    const data = settingsFixture(sendsWithHumbleFax);
    const { writes } = backend(data, status({ receiving: false, turned_on: false, reason: 'Receive faxes from HumbleFax is off.' }));
    render(<Settings client={client()} sections={['providers', 'humblefax']} canWrite />);
    const section = await screen.findByTestId('humblefax-receiving');
    expect(within(section).getByText('Faxes sent to your HumbleFax numbers stay only in your HumbleFax account.')).toBeTruthy();
    // Off and saved off: no interval, no status line and no Check now.
    expect(within(section).queryByRole('combobox')).toBeNull();
    expect(within(section).queryByRole('button', { name: 'Check now' })).toBeNull();
    fireEvent.click(within(section).getByRole('checkbox', { name: 'Receive faxes from HumbleFax' }));
    expect(within(section).getByText(
      'Faxbot collects the faxes your HumbleFax numbers receive, alongside any other way you receive.')).toBeTruthy();
    expect(within(section).getByText(/The first check also brings in the faxes HumbleFax received in the last 30 days/)).toBeTruthy();
    fireEvent.mouseDown(within(section).getByRole('combobox', { name: 'Check HumbleFax for received faxes' }));
    fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Every 5 minutes' }));
    // An unsaved switch says nothing about checking yet.
    expect(within(section).queryByTestId('humblefax-checked')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ expected_revision_id: expect.any(String), humblefax_receive_enabled: true,
      humblefax_poll_seconds: 300 });
    for (const label of Array.from(section.querySelectorAll('label'))) expect(label.textContent ?? '').not.toMatch(ENV_NAME);
  });

  it('says when Faxbot last checked and checks again now', async () => {
    const data = settingsFixture((value) => {
      sendsWithHumbleFax(value);
      value.humblefax = { ...value.humblefax, receive: true, poll_seconds: 120 };
    });
    const checkedAt = '2026-10-07T15:41:00';
    const { checks } = backend(data, status({ checked_at: checkedAt, found: 0, poll_seconds: 120 }),
      status({ checked_at: checkedAt, found: 2, poll_seconds: 120 }));
    render(<Settings client={client()} sections={['providers', 'humblefax']} canWrite />);
    const line = await screen.findByTestId('humblefax-checked');
    const time = new Date(`${checkedAt}Z`).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    expect(line.textContent).toBe(`Faxbot last checked HumbleFax at ${time} and found no new faxes.`);
    expect(screen.getByRole('combobox', { name: 'Check HumbleFax for received faxes' }).textContent).toBe('Every 2 minutes');
    fireEvent.click(screen.getByRole('button', { name: 'Check now' }));
    await waitFor(() => expect(screen.getByTestId('humblefax-checked').textContent).toBe(
      `Faxbot last checked HumbleFax at ${time} and found 2 new faxes.`));
    expect(checks).toHaveLength(1);
  });

  it('shows what stops receiving, and the reason Check now was refused', async () => {
    const data = settingsFixture((value) => {
      sendsWithHumbleFax(value);
      value.humblefax = { ...value.humblefax, receive: true, poll_seconds: 60 };
    });
    backend(data, status({ problem: 'HumbleFax rejected the account access key or secret key.' }),
      { status: 429, detail: 'HumbleFax asked Faxbot to slow down; select Check now again in a minute.' });
    render(<Settings client={client()} sections={['providers', 'humblefax']} canWrite />);
    expect((await screen.findByTestId('humblefax-problem')).textContent).toBe(
      'HumbleFax rejected the account access key or secret key.');
    fireEvent.click(screen.getByRole('button', { name: 'Check now' }));
    expect((await screen.findByTestId('humblefax-check-notice')).textContent).toBe(
      'HumbleFax asked Faxbot to slow down; select Check now again in a minute.');
  });

  it('needs no switch when HumbleFax is the receiving provider', async () => {
    const data = settingsFixture((value) => {
      value.backend.type = 'humblefax';
      value.hybrid = { outbound_backend: 'humblefax', inbound_backend: 'humblefax', outbound_override: '', inbound_override: '' };
      value.inbound.enabled = true;
    });
    backend(data, status({ receiving_provider: true }));
    render(<Settings client={client()} sections={['providers', 'humblefax']} canWrite />);
    const section = await screen.findByTestId('humblefax-receiving');
    expect(within(section).getByText('HumbleFax is your receiving provider, so Faxbot collects the faxes it receives.')).toBeTruthy();
    expect(within(section).queryByRole('checkbox', { name: 'Receive faxes from HumbleFax' })).toBeNull();
    expect((await within(section).findByTestId('humblefax-checked')).textContent).toBe('Faxbot has not checked HumbleFax yet.');
  });
});

describe('humblefaxStatusText', () => {
  it('gives one sentence for each state', () => {
    expect(humblefaxStatusText(status({ receiving: false, reason: 'Receiving faxes is turned off in Settings, so Faxbot is not checking HumbleFax.' })))
      .toBe('Receiving faxes is turned off in Settings, so Faxbot is not checking HumbleFax.');
    expect(humblefaxStatusText(status())).toBe('Faxbot has not checked HumbleFax yet.');
    expect(humblefaxStatusText(status({ checked_at: '2026-10-07T15:41:00', found: 1 }))).toMatch(/ and found 1 new fax\.$/);
  });
});
