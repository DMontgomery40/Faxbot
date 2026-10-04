import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import SetupWizard from '../components/SetupWizard';
import Settings from '../components/Settings';
import { server } from '../test/server';
import { receipt, settingsFixture, withDirections } from '../test/settingsFixture';

type Json = Record<string, any>;

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const RAW_IDS = /\b(efax|sip|phaxio|sinch|signalwire|documo|humblefax|freeswitch)\b/;
const ENV_NAME = /[A-Z]{3,}_[A-Z_]{2,}/;

function backend(data: Json) {
  const writes: Json[] = [];
  let revision = 1;
  server.use(
    http.get('/admin/settings', () => HttpResponse.json(data)),
    http.get('/plugins', () => HttpResponse.json({ items: [] })),
    http.get('/admin/tunnel/status', () => HttpResponse.json({ enabled: false, provider: 'none', status: 'disabled' })),
    http.put('/admin/settings', async ({ request }) => {
      const body = await request.json() as Json;
      writes.push(body);
      if (['backend', 'inbound_backend', 'inbound_enabled'].some((name) => name in body)) {
        const sending = body.backend ?? data.backend.type;
        const enabled = body.inbound_enabled ?? data.inbound.enabled;
        withDirections(data, sending, enabled ? (body.inbound_backend || sending) : '');
      }
      revision += 1;
      data._meta = { ...data._meta, desired_revision_id: `rev-${revision}`, active_revision_id: `rev-${revision}` };
      return HttpResponse.json(receipt(`rev-${revision}`));
    }),
  );
  return writes;
}

async function choose(name: 'Sending' | 'Receiving', option: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name }));
  fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: option }));
}

const next = () => fireEvent.click(screen.getByRole('button', { name: 'Next' }));
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe('eFax in the Setup Wizard', () => {
  it('offers eFax for sending and receiving and saves its account with the step', async () => {
    const writes = backend(settingsFixture());
    render(<SetupWizard client={client()} />);
    await screen.findByText('Choose Providers', { selector: 'h6' });
    await choose('Sending', 'eFax');
    await choose('Receiving', 'eFax');
    expect(screen.getByText('Sending: eFax · Receiving: eFax')).toBeTruthy();
    next();
    await screen.findByText('Connect Providers', { selector: 'h6' });
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', backend: 'efax', inbound_enabled: true }]);
    const section = await screen.findByTestId('provider-section-efax');
    expect(within(section).getByRole('heading', { level: 3 }).textContent).toBe('For sending and receiving: eFax');
    expect(within(section).getByTestId('efax-settings')).toBeTruthy();
    // eFax is asked for received faxes, so there is no callback address to set up.
    expect(screen.queryByRole('button', { name: 'Show callback details' })).toBeNull();
    expect(screen.getByText('Faxbot asks eFax for new faxes, so nothing needs to reach Faxbot from the internet.')).toBeTruthy();
    expect(screen.getByText('A copy stays in your eFax account.')).toBeTruthy();
    expect(screen.getByLabelText('Notification secret (optional)')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(RAW_IDS);
    for (const label of Array.from(document.querySelectorAll('label'))) expect(label.textContent ?? '').not.toMatch(ENV_NAME);

    type('App ID', 'synthetic-app');
    type('API key', 'synthetic-key-value');
    type('User ID', 'synthetic-user');
    type('Caller ID (optional)', '+13235551212');
    type('Station name (optional)', 'Front desk');
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Check eFax for received faxes' }));
    fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Every 5 minutes' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Delete each fax from eFax once Faxbot has stored it' }));
    expect(screen.getByText('eFax keeps no copy after Faxbot stores a fax.')).toBeTruthy();
    next();
    await screen.findByText('Security Settings', { selector: 'h6' });
    expect(writes[1]).toEqual({ expected_revision_id: 'rev-2', efax_app_id: 'synthetic-app', efax_api_key: 'synthetic-key-value',
      efax_user_id: 'synthetic-user', efax_caller_id: '+13235551212', efax_csid: 'Front desk', efax_poll_seconds: 300,
      efax_delete_after_download: true });
  });

  it('shows only the sending fields when eFax only sends, and keys set in .env as set there', async () => {
    const data = settingsFixture((fixture) => {
      withDirections(fixture, 'efax', 'sip');
      fixture.efax = { ...fixture.efax, app_id: '***', api_key: '***', user_id: '***', configured: true };
      fixture._meta.env_managed = ['efax_api_key'];
    });
    backend(data);
    server.use(http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [] })),
      http.get('/admin/sip/status', () => HttpResponse.json({ configured: false, message: 'No SIP trunk is set up.' })));
    render(<SetupWizard client={client()} />);
    await screen.findByText('Choose Providers', { selector: 'h6' });
    next();
    const section = await screen.findByTestId('provider-section-efax');
    expect(within(section).getByRole('heading', { level: 3 }).textContent).toBe('For sending: eFax');
    const key = within(section).getByLabelText('API key') as HTMLInputElement;
    expect(key.value).toBe('Set in .env');
    expect(key.disabled).toBe(true);
    expect((within(section).getByLabelText('App ID') as HTMLInputElement).disabled).toBe(false);
    expect(within(section).queryByRole('combobox', { name: 'Check eFax for received faxes' })).toBeNull();
    expect(within(section).queryByRole('checkbox', { name: /Delete each fax/ })).toBeNull();
    expect(within(section).queryByLabelText('Notification secret (optional)')).toBeNull();
  });
});

describe('eFax in Settings', () => {
  it('edits the eFax account and receiving choices without resending hidden keys', async () => {
    const writes = backend(settingsFixture((data) => {
      withDirections(data, 'efax', 'efax');
      data.efax = { app_id: '***', api_key: '***', user_id: '***', caller_id: '', csid: '', poll_seconds: 60,
        delete_after_download: false, configured: true };
    }));
    render(<Settings client={client()} />);
    const section = await screen.findByTestId('efax-settings');
    expect(await screen.findByText('Your eFax Enterprise API account')).toBeTruthy();
    expect(within(section).getByRole('combobox', { name: 'Check eFax for received faxes' }).textContent).toBe('Every minute');
    fireEvent.change(within(section).getByLabelText('Station name (optional)'), { target: { value: 'Clinic' } });
    fireEvent.click(within(section).getByRole('checkbox', { name: 'Delete each fax from eFax once Faxbot has stored it' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await screen.findByText('Settings saved.');
    expect(writes).toEqual([{ expected_revision_id: 'rev-a', efax_csid: 'Clinic', efax_delete_after_download: true }]);
  });

  it('is not shown when eFax is neither sending nor receiving', async () => {
    backend(settingsFixture());
    render(<Settings client={client()} />);
    await screen.findByText('Security Settings');
    expect(screen.queryByTestId('efax-settings')).toBeNull();
  });

  it('says how Faxbot checks eFax and which received faxes are still stored there', async () => {
    backend(settingsFixture((data) => {
      withDirections(data, 'efax', 'efax');
      data.efax = { ...data.efax, app_id: '***', api_key: '***', user_id: '***', configured: true };
    }));
    server.use(http.get('/admin/inbound/efax', () => HttpResponse.json({
      receiving: true, checked_at: '2026-10-04T15:30:00', problem: null, pending_deletions: 1, stopped_deletions: 0,
      notes: ['1 received fax is still stored at eFax; Faxbot will try again to delete it.'] })));
    render(<Settings client={client()} />);
    expect((await screen.findByTestId('efax-note')).textContent)
      .toBe('1 received fax is still stored at eFax; Faxbot will try again to delete it.');
    expect(screen.getByTestId('efax-checked').textContent).toMatch(/^Faxbot last checked eFax at .+\.$/);
    expect(screen.getByTestId('efax-checked').textContent).not.toMatch(/2026-10-04|T15/);
  });
});

describe('eFax prices where its API has no published price', () => {
  const usPlan = {
    provider_id: 'efax', country: 'US', page_url: 'https://www.efax.com/pricing', page_label: 'eFax’s US prices',
    sentence: 'eFax prices its API by quote. Its published plans start at USD 18.99 a month in the US (200 pages to the US and Canada, then 10¢ a page).',
    card: { provider_id: 'efax', label: 'eFax Personal plan, 200 pages a month (my estimate)', direction: 'outbound',
      currency: 'USD', per_minute: '0', per_page: '0', per_call: '0', billing_increment_seconds: 60, minimum_seconds: 0,
      source_url: 'https://www.efax.com/pricing', captured_on: '2026-10-04', monthly_fee: '18.99' },
  };

  async function renderCards(plans: Json) {
    const saved: Json[] = [];
    server.use(
      http.get('/routing/published-plans', () => HttpResponse.json(plans)),
      http.put('/routing/rate-cards', async ({ request }) => {
        saved.push(await request.json() as Json);
        return HttpResponse.json({ cards: [] });
      }),
    );
    const { default: RateCards } = await import('../components/delivery/RateCards');
    render(<RateCards client={client()} cards={[]} canWrite onChanged={() => undefined} unpriced={['efax']} />);
    return saved;
  }

  it('names the published plan and saves it as your own card only when asked', async () => {
    const saved = await renderCards(usPlan);
    const note = await screen.findByTestId('published-plans-efax');
    expect(note.textContent).toContain(usPlan.sentence);
    expect(saved).toEqual([]);
    fireEvent.click(within(note).getByRole('button', { name: 'Use a published plan as my estimate' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading').textContent).toBe('Add rate card');
    expect((within(dialog).getByLabelText('Name') as HTMLInputElement).value).toBe('eFax Personal plan, 200 pages a month (my estimate)');
    expect((within(dialog).getByLabelText('Monthly plan fee (USD)') as HTMLInputElement).value).toBe('18.99');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(saved).toEqual([{ cards: [usPlan.card] }]));
  });

  it('says the UK prices could not be read and links eFax’s UK page, with nothing to save', async () => {
    await renderCards({ provider_id: 'efax', country: 'GB', card: null, page_url: 'https://ww2.efax.com/uk/',
      page_label: 'eFax’s UK page', sentence: 'eFax prices its API by quote. Faxbot could not read eFax’s prices for the UK; see eFax’s UK page.' });
    const note = await screen.findByTestId('published-plans-efax');
    expect(within(note).getByRole('link', { name: 'eFax’s UK page' }).getAttribute('href')).toBe('https://ww2.efax.com/uk/');
    expect(within(note).queryByRole('button')).toBeNull();
  });
});
