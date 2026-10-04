// Numbers and Recipients: every number Faxbot carries with its mailbox, email and
// who can see it; sender identity; partners on private networks; case packets.
// Synthetic numbers only.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import ResourceAccess, { NO_MAILBOX, carriedNumbers, comparableNumber } from '../components/ResourceAccess';
import Settings from '../components/Settings';
import DeliveryRoutes from '../components/DeliveryRoutes';
import CasePackets from '../components/delivery/CasePackets';
import type { Settings as SettingsType } from '../api/types';
import { backend, server } from '../test/server';
import { receipt, settingsFixture, withDirections } from '../test/settingsFixture';

const keyClient = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

async function signedIn(...extra: string[]) {
  const admin = backend.state.principals.get('p_admin')!;
  admin.permissions = [...new Set([...admin.permissions, ...extra])];
  await AdminAPIClient.login('admin', 'correct horse');
  const client = new AdminAPIClient({ kind: 'session', csrf: null });
  return { client, me: await client.me() };
}

function carriedInstall() {
  const data = settingsFixture((value) => {
    withDirections(value, 'humblefax', 'sip');
    value.sip.trunk = { dids: ['+17208565062'] };
    value.humblefax.from_number = '3034265097';
  });
  backend.state.rules.set('rule_hf', { id: 'rule_hf', to_number: '+13034265097', mailbox_id: 'mbx_main', version: 1 });
  server.use(
    http.get('/admin/settings', () => HttpResponse.json(data)),
    http.get('/intake/connectors', () => HttpResponse.json({ connectors: [{
      id: 'c1', kind: 'email', name: 'Front desk', enabled: true, match_number: '+13034265097', host: 'smtp.clinic.example',
      port: 587, security: 'starttls', username: '', has_password: false, from_address: 'fax@clinic.example',
      recipients: ['frontdesk@clinic.example'], subject_template: 'Fax', managed: false, version: 1 }] })),
  );
  return data;
}

describe('Your numbers', () => {
  it('reads a number in one form, so a 10-digit HumbleFax number matches its +1 mailbox rule', () => {
    expect(comparableNumber('3034265097')).toBe('+13034265097');
    expect(comparableNumber('1 (303) 426-5097')).toBe('+13034265097');
    expect(comparableNumber('+44 121 234 5678')).toBe('+441212345678');
    const data = settingsFixture((value) => { withDirections(value, 'humblefax', ''); value.efax.caller_id = '+13035550100'; });
    const carried = carriedNumbers(data as unknown as SettingsType);
    expect(carried.map((entry) => [entry.number, entry.label, entry.inUse])).toEqual([['+13035550100', 'eFax', false]]);
  });

  it('lists every number Faxbot carries, with its mailbox or none, email delivery and who can see its faxes', async () => {
    carriedInstall();
    const { client, me } = await signedIn('settings:read');
    render(<ResourceAccess client={client} me={me} section="numbers" />);
    expect(await screen.findByRole('heading', { name: 'Your numbers' })).toBeTruthy();
    const trunk = (await screen.findByText('+17208565062')).closest('tr') as HTMLElement;
    expect(within(trunk).getByText('Carrier trunk')).toBeTruthy();
    expect(within(trunk).getByText(NO_MAILBOX)).toBeTruthy();
    expect(within(trunk).getByText('Not emailed')).toBeTruthy();
    expect(within(trunk).getByText('People with access to everything')).toBeTruthy();
    expect(within(trunk).getByRole('button', { name: 'Choose a mailbox for +17208565062' })).toBeTruthy();
    const humblefax = screen.getByText('+13034265097').closest('tr') as HTMLElement;
    expect(screen.getAllByText('+13034265097')).toHaveLength(1);
    expect(within(humblefax).getByText('HumbleFax')).toBeTruthy();
    expect(within(humblefax).getByText('Main line')).toBeTruthy();
    expect(within(humblefax).getByText('Emailed to frontdesk@clinic.example')).toBeTruthy();
    expect(within(humblefax).getByText('People with access to Main line, or to everything')).toBeTruthy();
  });

  it('offers a mailbox for a carried number without one, starting from that number', async () => {
    carriedInstall();
    const { client, me } = await signedIn('settings:read');
    render(<ResourceAccess client={client} me={me} section="numbers" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Choose a mailbox for +17208565062' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add fax number' });
    expect((within(dialog).getByLabelText('Fax number') as HTMLInputElement).value).toBe('+17208565062');
  });

  it('shows only numbers with a mailbox to people who may not read settings, and says so', async () => {
    carriedInstall();
    const admin = backend.state.principals.get('p_admin')!;
    admin.permissions = admin.permissions.filter((permission) => permission !== 'settings:read');
    await AdminAPIClient.login('admin', 'correct horse');
    const client = new AdminAPIClient({ kind: 'session', csrf: null });
    render(<ResourceAccess client={client} me={await client.me()} section="numbers" />);
    expect(await screen.findByText('+13034265097')).toBeTruthy();
    expect(screen.queryByText('+17208565062')).toBeNull();
    expect(screen.getByText('Numbers without a mailbox are shown only to people who can see settings.')).toBeTruthy();
  });
});

describe('Sender identity', () => {
  it('edits the header text and station ID on their own page, and the trunk page no longer shows the station ID', async () => {
    const writes: unknown[] = [];
    const data = settingsFixture((value) => { value.sender = { header: 'County Clinic', station_id: '+12025550100' }; });
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(data)),
      http.put('/admin/settings', async ({ request }) => { writes.push(await request.json()); return HttpResponse.json(receipt('rev-b')); }),
    );
    const { unmount } = render(<Settings client={keyClient()} sections={['identity']} title="Sender identity" canWrite />);
    expect(await screen.findByText('Header text')).toBeTruthy();
    expect(screen.getByText('Station ID')).toBeTruthy();
    expect(screen.getByText(/show the name and number set in that service's account/)).toBeTruthy();
    fireEvent.change(screen.getByDisplayValue('County Clinic'), { target: { value: 'Valley Clinic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({ fax_header: 'Valley Clinic' });
    unmount();
    render(<Settings client={keyClient()} sections={['trunk']} title="Carrier trunk" />);
    expect(await screen.findByText('SIP / Asterisk Configuration')).toBeTruthy();
    expect(screen.queryByText('Station ID')).toBeNull();
  });
});

describe('Partners', () => {
  it('offers partners on private networks as an advanced switch, off by default', async () => {
    const writes: Array<Record<string, unknown>> = [];
    const data = settingsFixture((value) => { value.direct = { enabled: true, organization: 'County Clinic', fax_number: '+12025550123', allow_private_peers: false }; });
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(data)),
      http.put('/admin/settings', async ({ request }) => { writes.push(await request.json() as Record<string, unknown>); return HttpResponse.json(receipt('rev-b')); }),
    );
    render(<Settings client={keyClient()} sections={['direct']} canWrite />);
    const toggle = await screen.findByRole('checkbox', { name: 'Allow partners on private networks (advanced)' });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(/Turn this on only for partners on a network you control/)).toBeTruthy();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0].direct_allow_private_peers).toBe(true);
  });
});

describe('Recipients', () => {
  it('says who each number is sent by first, whose partner it is and whether it takes an index', async () => {
    server.use(
      http.get('/routing/destinations', () => HttpResponse.json({ window_days: 30, destinations: [{
        number: '+15550100001', display_name: null, notes: null, preferred_route: null, accepts_references: true, version: 1,
        routes: [], estimated_cost_30_days: [] }] })),
      http.get('/direct/peers', () => HttpResponse.json({ peers: [{ id: 'p1', organization: 'Valley Hospital', fax_number: '+15550100001',
        endpoint: 'https://valley.example', state: 'verified', status: '', code_sent: false, code_expires_at: null, verified_at: null,
        expires_at: null, version: 1 }] })),
    );
    render(<DeliveryRoutes client={keyClient()} canWrite={false} section="numbers" />);
    const row = (await screen.findByText('+15550100001')).closest('tr') as HTMLElement;
    expect(within(row).getByText('Cheapest reliable')).toBeTruthy();
    expect(within(row).getByText('Valley Hospital')).toBeTruthy();
    expect(within(row).getByText('Takes a one-page list instead')).toBeTruthy();
  });
});

describe('Case packets', () => {
  const documents = { case_id: 'case-7', to: '+15550100001', accepts_references: true, documents: [
    { title: 'Referral', pages: 3, reference: 'abcdef123456', accepted: true, accepted_at: '2026-10-03T12:00:00', fax_id: 'f'.repeat(32) },
  ] };

  function caseServer(sends: Array<Record<string, string>>) {
    server.use(
      http.get('/cases/:caseId/documents', ({ params, request }) => {
        expect(params.caseId).toBe('case-7');
        expect(new URL(request.url).searchParams.get('to')).toBe('+15550100001');
        return HttpResponse.json(documents);
      }),
      // jsdom's FormData does not travel through the test fetch, so the client call itself is checked.
      http.post('/cases/:caseId/faxes', () => {
        const preview = sends.length === 0 || sends[sends.length - 1].preview === 'false';
        return HttpResponse.json({ case_id: 'case-7', to: '+15550100001', accepts_references: true, pages: 3, pages_saved: 2,
          documents: [{ title: 'Lab results', pages: 2, status: 'included' }, { title: 'Referral', pages: 3, status: 'referenced' }],
          fax_id: preview ? null : 'a'.repeat(32) }, { status: preview ? 200 : 202 });
      }),
    );
  }

  async function lookUp() {
    fireEvent.change(screen.getByLabelText('Case reference'), { target: { value: 'case-7' } });
    fireEvent.change(screen.getByLabelText('Recipient fax number'), { target: { value: '+15550100001' } });
    fireEvent.click(screen.getByRole('button', { name: 'Look up' }));
    return screen.findByTestId('case-documents');
  }

  it('shows what the recipient already has, previews what will be left out, then sends once', async () => {
    const sends: Array<Record<string, string>> = [];
    caseServer(sends);
    const api = keyClient();
    const original = api.sendCasePacket.bind(api);
    vi.spyOn(api, 'sendCasePacket').mockImplementation(async (caseId, to, files, preview) => {
      sends.push({ caseId, to, preview: String(preview), titles: files.map((file) => file.title).join('|') });
      return original(caseId, to, files, preview);
    });
    render(<CasePackets client={api} canSend canWrite={false} />);
    const held = await lookUp();
    expect(within(held).getByText('This recipient accepts a one-page list instead of documents it already has.')).toBeTruthy();
    expect(within(held).getByText('Referral')).toBeTruthy();
    expect(within(held).queryByText(/abcdef|ffffffff/)).toBeNull();
    const send = screen.getByRole('button', { name: 'Send the documents' }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);  // only after a preview
    fireEvent.change(screen.getByTestId('case-files'), { target: { files: [new File(['%PDF-1.4 lab'], 'Lab results.pdf', { type: 'application/pdf' })] } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    const plan = await screen.findByTestId('case-plan');
    expect(plan.textContent).toContain('3 pages will be sent; 2 pages left out.');
    expect(within(plan).getByText('Listed on the index')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Send the documents' }));
    expect((await screen.findByTestId('case-sent')).textContent).toContain('Queued to send: 3 pages, and 2 pages left out because the recipient already has them.');
    expect(sends).toEqual([
      { caseId: 'case-7', to: '+15550100001', preview: 'true', titles: 'Lab results' },
      { caseId: 'case-7', to: '+15550100001', preview: 'false', titles: 'Lab results' },
    ]);
  });

  it('shows the server sentence when nothing new is left to send, and no send part to people who may not send', async () => {
    const sends: Array<Record<string, string>> = [];
    caseServer(sends);
    server.use(http.post('/cases/:caseId/faxes', () => HttpResponse.json(
      { detail: 'Every document was already accepted for this case; there is nothing new to send.' }, { status: 409 })));
    const { unmount } = render(<CasePackets client={keyClient()} canSend canWrite={false} />);
    await lookUp();
    fireEvent.change(screen.getByTestId('case-files'), { target: { files: [new File(['%PDF-1.4 x'], 'Referral.pdf', { type: 'application/pdf' })] } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText('Every document was already accepted for this case; there is nothing new to send.')).toBeTruthy();
    unmount();
    render(<CasePackets client={keyClient()} canSend={false} canWrite={false} />);
    await lookUp();
    expect(screen.queryByTestId('case-send')).toBeNull();
  });
});
