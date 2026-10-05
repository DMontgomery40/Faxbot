import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import NetworkForFax, { actionSentence } from '../components/NetworkForFax';
import SipTrunkSettings from '../components/SipTrunkSettings';
import { server } from '../test/server';

const STEPS = [
  'colima version   # 0.9 or later keeps your faxes when the machine is recreated',
  'colima list',
  'colima delete default',
  'colima start default --cpu 2 --memory 4 --disk 20 --network-address --network-mode bridged '
    + '--network-interface "$(route -n get default | awk \'/interface:/{print $2}\')" --network-preferred-route',
  'docker compose up -d',
];

// What the server says for Faxbot in Colima's built-in network (measured 2026-10-04), with a documentation address.
const BLOCKED = {
  applies: true, checked: true, checked_at: '2026-10-05T03:50:00Z', platform: 'colima_user',
  platform_text: 'Faxbot runs in Docker on this Mac, on a private network inside the Mac.',
  ports: 'changed_per_destination', internet_address: '198.51.100.7', shared_address: false, t38: 'blocked',
  why: 'ports_change',
  text: 'Fax over IP (T.38) cannot work here: your network changes port numbers, which Telnyx cannot handle for fax '
    + 'over IP.',
  tries_text: null,
  fix_text: 'Connect Docker on this Mac directly to your local network with the commands below. Faxbot keeps its faxes '
    + 'and settings, Docker stops for a few minutes, and your Mac may ask for its password once.',
  fix_steps: STEPS,
  fix_note: 'Before you start: the second command lists the name and sizes to use (the name is default unless you '
    + "chose one). The disk keeps its old size. Run the last command in Faxbot's folder. Never add --data to the delete "
    + 'command, because that erases your faxes.',
  audio_text: 'Audio fax keeps working meanwhile.', t38_enabled: false, action: 'turned_off',
  action_at: '2026-10-05T03:50:00Z', fax_ports: '4000-4039', fax_ports_published: false, router_ports_enabled: true,
  router_state: 'not_needed', router_text: null,
};
const FIXED = {
  ...BLOCKED, platform: 'colima_bridged', platform_text: 'Faxbot runs in Docker on this Mac, directly on your local network.',
  ports: 'kept', t38: 'open', why: 'ports_kept',
  text: 'Fax over IP (T.38) can work here, because your network keeps port numbers unchanged.',
  tries_text: 'Faxbot tries fax over IP (T.38) first. When the carrier declines it, the fax goes through as audio.',
  fix_text: null, fix_steps: [], fix_note: null, audio_text: null, t38_enabled: true, action: 'turned_on',
  switched: 't38', engine_message: 'Saved for Asterisk. Restart the Asterisk service to use these settings.',
};
// A Linux computer behind a router that changes ports: Faxbot opened its published fax ports there.
const MAPPED = {
  ...FIXED, platform: 'linux_lan', platform_text: 'Faxbot runs on a computer on your local network, behind your router.',
  ports: 'changed_same', why: 'router_mapped',
  text: "Fax over IP (T.38) can work here, because your router passes Faxbot's fax ports through.",
  fax_ports_published: true, router_state: 'open', switched: null, engine_message: null,
  router_text: 'Faxbot opened UDP ports 4000–4039 on your router so fax data can come back. It renews them while it '
    + 'runs and closes them when it stops.',
};

function client() {
  return new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
}

describe('Network for fax over IP', () => {
  it('says why T.38 is off, what to run, and turns T.38 back on after Check again', async () => {
    let checks = 0;
    let changed = 0;
    server.use(
      http.get('/admin/sip/network', () => HttpResponse.json(BLOCKED)),
      http.post('/admin/sip/network/check', () => { checks += 1; return HttpResponse.json(FIXED); }),
    );
    render(<NetworkForFax client={client()} onChanged={() => { changed += 1; }} />);
    const section = await screen.findByTestId('sip-network');
    expect(within(section).getByText('Network for fax over IP')).toBeTruthy();
    expect(within(section).getByText(BLOCKED.text)).toBeTruthy();
    expect(within(section).getByText(BLOCKED.platform_text)).toBeTruthy();
    expect(within(section).getByText(/^On .+, Faxbot switched new calls to audio fax\.$/)).toBeTruthy();
    expect(within(section).getByText(BLOCKED.fix_text)).toBeTruthy();
    expect(within(section).getByTestId('sip-network-steps').textContent).toBe(STEPS.join('\n'));
    expect(within(section).getByText(BLOCKED.fix_note)).toBeTruthy();
    expect(within(section).getByText('Audio fax keeps working meanwhile.')).toBeTruthy();
    expect(within(section).getByText(/^Checked /)).toBeTruthy();
    // No developer words: no ISO times, no internal names.
    expect(section.textContent).not.toMatch(/2026-10-05T|colima_user|blocked|ports_change|not_needed/);
    fireEvent.click(within(section).getByRole('button', { name: 'Check again' }));
    expect(await within(section).findByText(FIXED.text)).toBeTruthy();
    // Network open is not carrier acceptance: T.38 is tried first, audio carries the fax when the carrier declines.
    expect(within(section).getByText(FIXED.tries_text)).toBeTruthy();
    expect(within(section).getByText(
      /^On .+, Faxbot switched new calls back to fax over IP \(T\.38\), because your network allows it now\.$/)).toBeTruthy();
    expect(within(section).getByText(FIXED.engine_message)).toBeTruthy();
    expect(within(section).queryByText('Audio fax keeps working meanwhile.')).toBeNull();
    expect(within(section).queryByTestId('sip-network-steps')).toBeNull();
    expect(checks).toBe(1);
    await waitFor(() => expect(changed).toBe(1));
  });

  it('says which ports Faxbot opened on the router, and turning that off saves the setting and checks again', async () => {
    const writes: Array<Record<string, unknown>> = [];
    let checks = 0;
    server.use(
      http.get('/admin/sip/network', () => HttpResponse.json(MAPPED)),
      http.get('/admin/settings', () => HttpResponse.json({ _meta: { active_revision_id: 'rev-1',
        desired_revision_id: 'rev-1', generation: 1, apply_state: 'applied', pending_fields: [] } })),
      http.put('/admin/settings', async ({ request }) => {
        writes.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ok: true, changed: true, _meta: { active_revision_id: 'rev-2',
          desired_revision_id: 'rev-2', generation: 2, apply_state: 'applied', restart_recommended: false } });
      }),
      http.post('/admin/sip/network/check', () => {
        checks += 1;
        return HttpResponse.json({ ...BLOCKED, platform: 'linux_lan', router_ports_enabled: false, router_state: 'off',
          router_text: 'Faxbot does not ask your router to open ports, because that is turned off.' });
      }),
    );
    render(<NetworkForFax client={client()} />);
    const section = await screen.findByTestId('sip-network');
    expect(within(section).getByText(MAPPED.router_text)).toBeTruthy();
    const toggle = within(section).getByRole('checkbox', { name: 'Let Faxbot open its fax ports on your router' });
    expect((toggle as HTMLInputElement).checked).toBe(true);
    fireEvent.click(toggle);
    expect(await within(section).findByText('Faxbot does not ask your router to open ports, because that is turned off.'))
      .toBeTruthy();
    expect(writes).toEqual([{ expected_revision_id: 'rev-1', sip_router_ports: false }]);
    expect(checks).toBe(1);
    expect((within(section).getByRole('checkbox', { name: 'Let Faxbot open its fax ports on your router' }) as HTMLInputElement)
      .checked).toBe(false);
  });

  it('shows nothing for a phone system or without a carrier, and says when Check again is not allowed', async () => {
    const { container } = render(<NetworkForFax client={client()} />);
    await waitFor(() => expect(container.textContent).toBe(''));
    server.use(
      http.get('/admin/sip/network', () => HttpResponse.json(BLOCKED)),
      http.post('/admin/sip/network/check', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })),
    );
    render(<NetworkForFax client={client()} />);
    const section = await screen.findByTestId('sip-network');
    fireEvent.click(within(section).getByRole('button', { name: 'Check again' }));
    expect(await within(section).findByText('You do not have permission to do this.')).toBeTruthy();
  });

  it('is part of the carrier trunk page', async () => {
    server.use(
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [
        { id: 'telnyx', label: 'Telnyx', host: 'sip.telnyx.com', port: 5061, transport: 'tls',
          auth_modes: ['registration'], codecs: ['ulaw', 'alaw'], needs_host: false, ip_dial_prefix: false, t38: '',
          notes: [], sources: [], kind: 'carrier', transports: ['tls', 'tcp', 'udp'] }] })),
      http.get('/admin/settings', () => HttpResponse.json({
        _meta: { active_revision_id: 'rev-1', desired_revision_id: 'rev-1', generation: 1, apply_state: 'applied',
          pending_fields: [] },
        sip: { trunk: { preset: 'telnyx', auth: 'registration', host: '', port: 0, transport: '', username: 'faxbotuser',
          password: '***', password_set: true, outbound_proxy: '', caller_id: '+15555550100', dids: [],
          t38_enabled: false, fax_preference_header: true, codecs: '', t38_off_reason: 'network',
          t38_off_at: '2026-10-05T03:50:00Z', router_ports: true } } })),
      http.get('/admin/sip/network', () => HttpResponse.json(BLOCKED)),
    );
    render(<SipTrunkSettings client={client()} showCalls={false} />);
    const section = await screen.findByTestId('sip-network');
    expect(within(section).getByText(BLOCKED.text)).toBeTruthy();
    expect(await screen.findByText('Off: your network changes port numbers, so fax over IP (T.38) cannot work; Faxbot '
      + 'sends audio fax until the network is fixed.')).toBeTruthy();
  });

  it('words what Faxbot did with the day only', () => {
    expect(actionSentence('turned_off', null)).toBe('Faxbot switched new calls to audio fax.');
    expect(actionSentence('turned_on', 'not a time')).toBe(
      'Faxbot switched new calls back to fax over IP (T.38), because your network allows it now.');
    expect(actionSentence(null, null)).toBeNull();
  });
});
