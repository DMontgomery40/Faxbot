import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import ReceivingReadiness from '../components/ReceivingReadiness';
import PowerCheck from '../components/PowerCheck';
import type { Power, Readiness } from '../api/siteChecks';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const notReceiving: Readiness = {
  checked_text: '10 Oct 9:00 AM MDT', receiving_on: true,
  numbers: [{
    number: '+13035550121', status: 'not_receiving', owner: null, owner_label: null,
    sentence: 'Not receiving: Faxbot could not reach its own receiving address for Sinch through your public address, '
      + 'so faxes from it cannot arrive. Check the public address and any tunnel in front of Faxbot.',
    endpoints: [{ key: 'sinch', label: 'Sinch', provider: 'sinch' }, { key: 'sip', label: 'Telnyx trunk', provider: 'sip' }],
    checks: [{ title: 'Last fax received', status: 'off', sentence: 'No fax to this number has arrived yet.' }],
    last_received_text: null,
  }],
};

const power = (overrides: Partial<Power> = {}): Power => ({
  configured: false, host: null, port: 3493, ups_name: null, reserve_minutes: 2, status: 'off',
  sentence: 'No UPS is set. Set its address so Faxbot holds long calls while the office runs on battery.',
  on_battery: false, runtime_minutes: null, charge_percent: null, ...overrides,
});

describe('Receiving readiness in Diagnostics', () => {
  it('says a number is not receiving and names its receiver on request', async () => {
    let saved: unknown = null;
    server.use(http.get('/receiving/readiness', () => HttpResponse.json(notReceiving)),
      http.post('/receiving/owners', async ({ request }) => {
        saved = await request.json();
        return HttpResponse.json({ sentence: 'Sinch now receives the faxes for +13035550121.' });
      }));
    render(<ReceivingReadiness client={client()} />);
    expect(await screen.findByText('+13035550121: Not receiving')).toBeTruthy();
    expect(screen.getByText(notReceiving.numbers[0].sentence)).toBeTruthy();
    const card = screen.getByTestId('receiving-readiness');
    fireEvent.mouseDown(within(card).getByLabelText('Receives its faxes'));
    fireEvent.click(await screen.findByRole('option', { name: 'Sinch' }));
    fireEvent.click(within(card).getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Sinch now receives the faxes for +13035550121.')).toBeTruthy();
    expect(saved).toEqual({ number: '+13035550121', owner: 'sinch', label: null, move: false });
  });

  it('shows the refusal when another receiver already owns the number', async () => {
    const owned = { ...notReceiving, numbers: [{ ...notReceiving.numbers[0], owner: 'sip', owner_label: 'Telnyx trunk' }] };
    server.use(http.get('/receiving/readiness', () => HttpResponse.json(owned)),
      http.post('/receiving/owners', () => HttpResponse.json({ detail: 'Telnyx trunk already receives the faxes for '
        + '+13035550121. Move the number to the new receiver on purpose, or release it first, so two places never '
        + 'take its faxes without you knowing.' }, { status: 409 })));
    render(<ReceivingReadiness client={client()} />);
    const card = await screen.findByTestId('receiving-readiness');
    await screen.findByText('+13035550121: Not receiving');
    fireEvent.mouseDown(within(card).getByLabelText('Receives its faxes'));
    fireEvent.click(await screen.findByRole('option', { name: 'Sinch' }));
    fireEvent.click(within(card).getByRole('button', { name: 'Move the number here' }));
    expect(await screen.findByText(/Telnyx trunk already receives the faxes for \+13035550121\./)).toBeTruthy();
  });

  it('says when receiving is off and when it cannot check', async () => {
    render(<ReceivingReadiness client={client()} />);
    expect(await screen.findByText('Receiving faxes is off on this installation.')).toBeTruthy();
    server.use(http.get('/receiving/readiness', () => HttpResponse.json({ detail: 'x' }, { status: 503 })));
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    expect(await screen.findByText('Faxbot could not check receiving. Check again in a moment.')).toBeTruthy();
  });
});

describe('Power in Diagnostics', () => {
  it('sets the UPS, shows what it says, and stops reading it', async () => {
    const puts: unknown[] = [];
    server.use(http.get('/power', () => HttpResponse.json(power())),
      http.put('/power', async ({ request }) => {
        const body = await request.json() as { host: string };
        puts.push(body);
        return HttpResponse.json(body.host ? power({ configured: true, host: body.host, status: 'attention', on_battery: true,
          sentence: 'On battery, with about 10 minutes left. Faxbot starts a call on this server only when it can '
            + 'finish with 2 minutes to spare.' }) : power());
      }));
    render(<PowerCheck client={client()} />);
    expect(await screen.findByText(/No UPS is set\./)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('UPS server address'), { target: { value: '192.168.1.5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText(/On battery, with about 10 minutes left\./)).toBeTruthy();
    expect(puts[0]).toEqual({ host: '192.168.1.5', port: 3493, ups_name: null, reserve_minutes: 2 });
    fireEvent.click(screen.getByRole('button', { name: 'Stop reading the UPS' }));
    await waitFor(() => expect(puts[1]).toEqual({ host: '' }));
    expect(await screen.findByText('Faxbot no longer reads a UPS.')).toBeTruthy();
  });

  it('refuses a port out of range before asking the server', async () => {
    const puts: unknown[] = [];
    server.use(http.put('/power', async ({ request }) => { puts.push(await request.json()); return HttpResponse.json(power()); }));
    render(<PowerCheck client={client()} />);
    await screen.findByText(/No UPS is set\./);
    fireEvent.change(screen.getByLabelText('UPS server address'), { target: { value: 'ups.local' } });
    fireEvent.change(screen.getByLabelText('Port'), { target: { value: '70000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Enter a port from 1 to 65535; NUT uses 3493.')).toBeTruthy();
    expect(puts).toEqual([]);
  });
});
