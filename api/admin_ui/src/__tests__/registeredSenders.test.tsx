// Recipients → Registered senders, Sent details → Sender's evidence, and the encrypted audio trunk on the trunk
// screen. Synthetic numbers only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import RegisteredSenders, { SenderEvidenceItem } from '../components/delivery/RegisteredSenders';
import SipTrunkSettings, { audioReason } from '../components/SipTrunkSettings';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const BANK = '+902122220000';
const TRUNKS = [{ account: 'sip', label: 'Telnyx', caller_id: '+13035550100', station_id: '+13035550100' }];
const READY = { recipient: BANK, account: 'sip', account_label: 'Telnyx', caller_id: '+13035550100',
  station_id: '+13035550100', note: 'Bank form 7.4', recorded_by: 'Anne', recorded_at: '2026-10-09T12:00:00',
  ready: true, sentence: `Faxes to ${BANK} go only by Telnyx, showing +13035550100.` };

describe('Registered senders', () => {
  it('lists them, adds one with the trunk it is registered with, and removes one', async () => {
    const sent: unknown[] = [];
    server.use(
      http.get('/routing/sender-pins', () => HttpResponse.json({ pins: [], trunks: TRUNKS })),
      http.put('/routing/sender-pins/:number', async ({ request, params }) => {
        sent.push({ number: params.number, body: await request.json() });
        return HttpResponse.json({ pins: [READY], trunks: TRUNKS });
      }),
      http.delete('/routing/sender-pins/:number', () => HttpResponse.json({ pins: [], trunks: TRUNKS })),
    );
    render(<RegisteredSenders client={client()} canWrite />);
    expect((await screen.findByTestId('registered-senders')).textContent).toContain('No registered senders.');
    fireEvent.click(screen.getByRole('button', { name: 'Add a registered sender' }));
    const dialog = await screen.findByRole('dialog');
    expect((within(dialog).getByLabelText('Registered caller ID') as HTMLInputElement).value).toBe('+13035550100');
    fireEvent.change(within(dialog).getByLabelText("Recipient's fax number"), { target: { value: BANK } });
    fireEvent.change(within(dialog).getByLabelText('Note'), { target: { value: 'Bank form 7.4' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByText('Ready')).toBeTruthy());
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(sent).toEqual([{ number: BANK, body: { account: 'sip', caller_id: '+13035550100', station_id: null,
      note: 'Bank form 7.4' } }]);
    fireEvent.click(screen.getByRole('button', { name: `Remove the registered sender for ${BANK}` }));
    await waitFor(() => expect(screen.getByText('No registered senders.')).toBeTruthy());
  });

  it('says why faxes wait when the trunk would not show the registered number, and offers no change to readers', async () => {
    const waiting = { ...READY, ready: false,
      sentence: `Faxes to ${BANK} wait in Sent: Telnyx shows +13035550142 as caller ID now, not +13035550100.` };
    server.use(http.get('/routing/sender-pins', () => HttpResponse.json({ pins: [waiting], trunks: TRUNKS })));
    render(<RegisteredSenders client={client()} canWrite={false} />);
    expect((await screen.findByRole('alert')).textContent).toBe(waiting.sentence);
    expect(screen.queryByRole('button', { name: 'Add a registered sender' })).toBeNull();
    expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull();
  });

  it("shows the sender's evidence in Sent details and records a request for the original", async () => {
    const evidence = { job_id: 'a'.repeat(32), recipient: BANK,
      pin: { caller_id: '+13035550100', station_id: '+13035550100', account: 'sip' }, kept: ['fax image'],
      calls: [{ caller_id: '+13035550100', answering_station: 'GARANTI 1', pages: 2, fax_status: 'success',
        disposition: 'answered', matches_pin: true }], original: null };
    const posted: unknown[] = [];
    server.use(
      http.get('/routing/faxes/:id/sender-evidence', () => HttpResponse.json(evidence)),
      http.post('/routing/faxes/:id/original', async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json({ ...evidence, original: 'requested' });
      }),
    );
    render(<ul><SenderEvidenceItem client={client()} jobId={'a'.repeat(32)} /></ul>);
    const item = await screen.findByTestId('sender-evidence');
    expect(item.textContent).toContain(`Sent from +13035550100, the number registered with ${BANK}. Answered by GARANTI 1. Kept: fax image.`);
    fireEvent.click(within(item).getByRole('button', { name: 'The recipient asked for the original' }));
    await waitFor(() => expect(screen.getByTestId('sender-evidence').textContent)
      .toContain('The recipient asked for the original.'));
    expect(posted).toEqual([{ state: 'requested', note: '' }]);
    expect(within(screen.getByTestId('sender-evidence')).getByRole('button', { name: 'The original was sent' })).toBeTruthy();
  });

  it('shows nothing in Sent details for any other fax', async () => {
    const shown = render(<ul><SenderEvidenceItem client={client()} jobId={'b'.repeat(32)} /></ul>);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(shown.container.textContent).toBe('');
  });
});

const COMPANYFLEX = { id: 'telekom-companyflex', label: 'Telekom CompanyFlex', host: 'tel.t-online.de', port: 5061,
  transport: 'tls', auth_modes: ['registration'], codecs: ['alaw', 'ulaw'], needs_host: false, ip_dial_prefix: false,
  t38: 'On your Telekom line Telekom recommends T.38.', notes: [], sources: [], transports: ['tls', 'tcp'],
  media_encryption: 'sdes', access_rule: 'telekom' };
const META = { active_revision_id: 'rev-1', desired_revision_id: 'rev-1', generation: 1, apply_state: 'applied',
  pending_fields: [] };

describe('Encrypted audio fax trunks', () => {
  it("says why calls are encrypted and saves the Telekom line's internet address", async () => {
    const writes: Array<Record<string, unknown>> = [];
    const sentence = 'Faxbot is not on the Telekom line you listed, so CompanyFlex requires encrypted calls.';
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [COMPANYFLEX] })),
      http.get('/admin/settings', () => HttpResponse.json({ _meta: META, sip: { trunk: {
        preset: 'telekom-companyflex', auth: 'registration', host: '', port: 0, transport: 'tcp', username: 'k1',
        password: '***', password_set: true, outbound_proxy: 'k1.primary.companyflex.de', caller_id: '+493055500100',
        dids: [], t38_enabled: false, fax_preference_header: true, codecs: '', own_access: '',
        media_encryption: 'sdes', access: 'other', encryption_sentence: sentence, t38_off_reason: 'encrypted' } } })),
      http.get('/admin/sip/calls', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ok: true, changed: true, _meta: { ...META, active_revision_id: 'rev-2',
          desired_revision_id: 'rev-2', restart_recommended: false } });
      }),
    );
    render(<SipTrunkSettings client={client()} />);
    expect((await screen.findByTestId('trunk-encryption')).textContent).toBe(sentence);
    fireEvent.change(screen.getByLabelText("Your own line's internet address"), { target: { value: '198.51.100.7' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save trunk settings' }));
    await waitFor(() => expect(writes).toEqual([{ expected_revision_id: 'rev-1', sip_trunk_own_access: '198.51.100.7' }]));
  });

  it('says why an encrypted trunk sends audio fax', () => {
    expect(audioReason('encrypted', null, 'Swisscom Smart Business Connect')).toBe(
      'Off: calls over Swisscom Smart Business Connect are encrypted here, and fax over IP (T.38) cannot be encrypted, so Faxbot sends encrypted audio fax.');
  });
});
