import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import TrunkAccountPanel from '../components/TrunkAccountPanel';
import SendOnlyNumbers from '../components/SendOnlyNumbers';
import TrunkAdvice from '../components/delivery/TrunkAdvice';
import type AdminAPIClient from '../api/client';
import { FakeRules } from './providerRulesFake';

type Request = { method: string; path: string; body?: unknown };

describe('a trunk after the first', () => {
  it('shows its connection, its settings and what is wrong, and changes it through the account', async () => {
    const fake = new FakeRules();
    fake.accounts.accounts.push({
      ...fake.accounts.accounts[0], key: 'sip-leeds', label: 'Leeds trunk (Gamma)', primary: false,
      numbers: ['+441132000000'], limits: { ...fake.accounts.accounts[0].limits, at_once: 1 },
      settings: { host: '192.0.2.40', auth: 'ip', caller_id: '+441132000000', t38: false },
    });
    const call = vi.fn(async (request: Request) => {
      expect(request.path).toBe('/admin/sip/status?account=sip-leeds');
      return {
        message: 'The trunk is ready.', preset_label: 'Gamma', registration_text: null,
        reachability_text: 'Gamma answers Faxbot\'s checks.', trunk_problems: {},
        telnyx_t38: null,
      };
    }) as unknown as <T>(request: Request) => Promise<T>;
    render(<TrunkAccountPanel api={fake.api()} call={call} accountKey="sip-leeds" />);
    expect(await screen.findByText('Leeds trunk (Gamma)')).toBeTruthy();
    expect(screen.getByText('The trunk is ready.')).toBeTruthy();
    expect(screen.getByText('Server: 192.0.2.40')).toBeTruthy();
    expect(screen.getByText('Receives on +441132000000')).toBeTruthy();
    expect(screen.getByText('1 line at once')).toBeTruthy();
    expect(screen.getByText('Fax over IP (T.38) off: audio fax only')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Change this trunk' })).toBeTruthy();
  });

  it('says why a trunk is not loaded', async () => {
    const fake = new FakeRules();
    fake.accounts.accounts.push({ ...fake.accounts.accounts[0], key: 'sip-half', label: 'Half trunk', primary: false });
    const call = (async () => ({
      message: 'No SIP trunk is set up.', trunk_problems: { 'sip-half': 'Half trunk is not loaded yet: fill in its settings.' },
    })) as unknown as <T>(request: Request) => Promise<T>;
    render(<TrunkAccountPanel api={fake.api()} call={call} accountKey="sip-half" />);
    expect(await screen.findByText('Half trunk is not loaded yet: fill in its settings.')).toBeTruthy();
  });
});

describe('send-only numbers', () => {
  it('lists them with the carrier rule, adds one, and shows the reason when one is refused', async () => {
    let numbers = ['+13035550142'];
    const sent: unknown[] = [];
    const call = vi.fn(async (request: Request) => {
      if (request.method === 'PUT') {
        sent.push(request.body);
        const wanted = (request.body as { numbers: string[] }).numbers;
        if (wanted.includes('+17205550101')) {
          throw { detail: '+17205550101 receives faxes on Telnyx, so it cannot be send-only.' };
        }
        numbers = wanted;
        return { ok: true, numbers };
      }
      return {
        quiet_days: 90,
        numbers: numbers.map((number) => ({
          number, sentence: 'Shown as caller ID on Telnyx. Faxbot never receives on it and never treats it as one of your fax numbers.',
          carrier_rules: [{ trunk: 'Telnyx', sentence: 'Telnyx shows another number only when it was verified.',
            source_url: 'https://support.telnyx.com/en/articles/6790265-verified-numbers-faq', read_on: '2026-10-07' }],
        })),
        advice: [{ number: '+17205550101', source_url: null,
          sentence: 'You rent +17205550101 on Telnyx for $1.00 a month and use it only to show on faxes you send.' }],
      };
    }) as unknown as <T>(request: Request) => Promise<T>;
    render(<SendOnlyNumbers call={call} />);
    expect(await screen.findByText('+13035550142')).toBeTruthy();
    expect(screen.getByText(/Telnyx shows another number only when it was verified/)).toBeTruthy();
    expect(screen.getByText(/You rent \+17205550101 on Telnyx/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Add a send-only number'), { target: { value: '+17205550101' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add send-only number' }));
    expect(await screen.findByText('+17205550101 receives faxes on Telnyx, so it cannot be send-only.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Add a send-only number'), { target: { value: '+13035550199' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add send-only number' }));
    expect(await screen.findByText('+13035550199')).toBeTruthy();
    expect(sent[sent.length - 1]).toEqual({ numbers: ['+13035550142', '+13035550199'] });
  });
});

describe('trunk advice', () => {
  it('compares the trunks and says what moving one would save', async () => {
    const client = {
      call: async () => ({
        window_days: 30, sentence: null,
        trunks: [
          { key: 'sip', label: 'Telnyx', lines: 2, peak_lines: 1, sent: 40, received: 12, monthly: '$0.00', cost_per_delivered: '$0.01' },
          { key: 'sip-leeds', label: 'Leeds trunk', lines: 1, peak_lines: 1, sent: 14, received: 0, monthly: '22.00 GBP', cost_per_delivered: '0.02 GBP' },
        ],
        items: [{ trunk: 'sip-leeds', into: 'sip', sentence: 'Sending those faxes over Telnyx and cancelling Leeds trunk would save about 22.00 GBP a month.' }],
      }),
    } as unknown as AdminAPIClient;
    let count: number | null = -1;
    render(<TrunkAdvice client={client} onCount={(value) => { count = value; }} />);
    expect(await screen.findByText(/would save about 22.00 GBP a month/)).toBeTruthy();
    expect(screen.getByRole('table', { name: 'Your trunks' }).textContent).toContain('Leeds trunk');
    expect(count).toBe(1);
  });

  it('stays out of the way with one trunk', async () => {
    const client = { call: async () => ({ window_days: 30, trunks: [{ key: 'sip', label: 'Telnyx' }], items: [],
      sentence: 'Trunk advice needs two or more trunks; with one, there is nothing to move.' }) } as unknown as AdminAPIClient;
    const { container } = render(<TrunkAdvice client={client} />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(container.textContent).toBe('');
  });
});
