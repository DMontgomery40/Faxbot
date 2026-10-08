// Number advice: where each number should live with porting steps, your NPI record, calls by state, and the Send
// screen's check before a first fax, which warns and never stops the fax.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { NpiRecord, NumberPlacement as Placement, SiteAdvice as Advice } from '../api/numberAdviceTypes';
import NumberPlacement from '../components/delivery/NumberPlacement';
import SiteAdvice from '../components/delivery/SiteAdvice';
import SendFax from '../components/SendFax';
import { server } from '../test/server';

type Request = { method: string; path: string; body?: unknown };
const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const usd = (amount: string) => [{ currency: 'USD', amount }];

const steps = {
  from: 'Telnyx', to: 'AnveoDirect',
  steps: ['At Telnyx: give the new carrier one of the numbers being moved as the account number, and the port-out PIN.',
    'At AnveoDirect: Fill in Anveo\'s porting form and email it with your latest bill to LNP@ANVEO.COM.',
    'Keep +1 303-555-0101 and your Telnyx account active until AnveoDirect confirms the date the move completes; never cancel first, or the number can be lost.',
    'When it completes, add +1 303-555-0101 to your AnveoDirect account in Faxbot under Providers and remove it from Telnyx.'],
  fee: 'AnveoDirect: $15 a US or Canadian number, local or toll-free.',
  lead_time: 'About 25 business days for US numbers (3 to 6 weeks on average).',
  restriction: null,
  sources: [{ label: 'Telnyx: port numbers away', url: 'https://support.telnyx.com/en/articles/2033789-port-numbers-away-from-telnyx',
    read_on: '2026-10-08', secondary: false }],
};

const placement: Placement = {
  days: 30, estimate: true, state: 'advice',
  sentence: '1 of your 2 numbers would cost less at another of your accounts (estimate).',
  numbers: [
    { number: '+13035550101', display: '+1 303-555-0101', kind: 'local', account: 'Telnyx', received: 0, pages: 0,
      costs: [{ account: 'Telnyx', current: true, monthly: usd('1.00'), faxes: usd('0.00'), number_fee: usd('1.00'), reason: null }],
      skipped: [], notes: [], npi_record: null, cheapest: 'AnveoDirect trunk', saving: usd('0.85'), porting: steps,
      state: 'move', sentence: 'Move +1 303-555-0101 from Telnyx to AnveoDirect trunk: about $0.85 a month less (estimate), from 0 faxes received in the last 30 days.' },
    { number: '+13035550100', display: '+1 303-555-0100', kind: 'local', account: 'Telnyx', received: 40, pages: 200,
      costs: [{ account: 'Telnyx', current: true, monthly: usd('1.38'), faxes: usd('0.38'), number_fee: usd('1.00'), reason: null }],
      skipped: [], notes: [], npi_record: null, cheapest: null, saving: [], porting: null, state: 'keep',
      sentence: 'Keep +1 303-555-0100 at Telnyx: it costs about $1.38 a month there (estimate), the least of your accounts.' },
  ],
  accounts: [], note: 'Faxbot only advises: it never moves, releases or cancels a number or an account.',
};

const record: NpiRecord = { npis: [], sentence: 'Add your NPI so Faxbot can tell you when a number you might give up is still printed on your NPI record.',
  source_url: 'https://npiregistry.cms.hhs.gov/api-page' };
const read: NpiRecord = { sentence: 'Faxbot last read your NPI record on October 8, 2026.', source_url: record.source_url,
  npis: [{ npi: '1234567893', label: 'Denver office', name: 'OUR SYNTHETIC PRACTICE', read_at: '2026-10-08T12:00:00',
    numbers: [{ number: '+17205550199', display: '+1 720-555-0199', kind: 'fax', where: 'practice location', address: null }] }] };

function fakeClient(answers: Record<string, unknown>, requests: Request[] = []) {
  const call = vi.fn(async (request: Request) => {
    requests.push(request);
    const key = `${request.method} ${request.path}`;
    if (!(key in answers)) throw new Error(`unexpected ${key}`);
    return answers[key];
  });
  return { call } as unknown as AdminAPIClient;
}

describe('where each number should live', () => {
  it('names the cheaper account with the steps to move the number, and adds your NPI', async () => {
    const requests: Request[] = [];
    const onCount = vi.fn();
    render(<NumberPlacement client={fakeClient({ 'GET /routing/recommendations/numbers': placement, 'GET /routing/npi': record,
      'POST /routing/npi': read }, requests)} canWrite onCount={onCount} />);
    const panel = await screen.findByTestId('number-placement');
    expect(within(panel).getByText(placement.sentence)).toBeTruthy();
    expect(within(panel).getByText(placement.numbers[0].sentence)).toBeTruthy();
    expect(within(panel).getByText('To move it from Telnyx to AnveoDirect:')).toBeTruthy();
    expect(within(panel).getByText(`Fee: ${steps.fee}`)).toBeTruthy();
    expect(within(panel).getByRole('link', { name: 'Telnyx: port numbers away' })).toBeTruthy();
    // A number that stays where it is needs no sentence of its own beyond the table.
    expect(within(panel).queryByText(placement.numbers[1].sentence)).toBeNull();
    expect(onCount).toHaveBeenCalledWith(1);
    const npi = await screen.findByTestId('npi-record');
    expect(within(npi).getByText(record.sentence)).toBeTruthy();
    fireEvent.change(screen.getByTestId('npi-input'), { target: { value: '1234567893' } });
    fireEvent.change(within(npi).getByRole('textbox', { name: 'Location (optional)' }), { target: { value: 'Denver office' } });
    fireEvent.click(within(npi).getByRole('button', { name: 'Add NPI' }));
    expect(await within(npi).findByText('NPI 1234567893 (Denver office): OUR SYNTHETIC PRACTICE')).toBeTruthy();
    expect(within(npi).getByText('+1 720-555-0199')).toBeTruthy();
    expect(requests[requests.length - 1]).toEqual({ method: 'POST', path: '/routing/npi', body: { npi: '1234567893', label: 'Denver office' } });
  });

  it('shows no changes to someone who may only read settings', async () => {
    render(<NumberPlacement client={fakeClient({ 'GET /routing/recommendations/numbers': placement, 'GET /routing/npi': read })} />);
    const npi = await screen.findByTestId('npi-record');
    expect(within(npi).queryByRole('button', { name: 'Add NPI' })).toBeNull();
    expect(within(npi).queryByRole('button', { name: 'Remove this NPI' })).toBeNull();
  });
});

describe('calls by state', () => {
  it('says a flat carrier changes nothing and never offers to change caller ID', async () => {
    const advice: Advice = { days: 30, estimate: true, sentence: 'Telnyx publishes one price for US calls, so where a call starts makes no difference.',
      carriers: [{ account: 'sip', carrier: 'Telnyx', by_jurisdiction: false, sentence: 'Telnyx publishes one price for US calls, so where a call starts makes no difference.' }],
      items: [], prices: [], caller_id: 'Faxbot never changes caller ID to lower call charges (FCC, Truth in Caller ID; 47 CFR 64.1601).' };
    render(<SiteAdvice client={fakeClient({ 'GET /routing/recommendations/sites': advice })} />);
    const panel = await screen.findByTestId('site-advice');
    expect(within(panel).getAllByText(advice.sentence)).toHaveLength(1);
    expect(within(panel).getByText(advice.caller_id)).toBeTruthy();
    expect(within(panel).queryByTestId('state-prices-import')).toBeNull();
  });

  it('names the site whose trunk costs less for a state', async () => {
    const item = { from_site: 'Denver', to_site: 'Salt Lake City', state: 'UT', state_name: 'Utah', faxes: 20, saving: usd('0.40'),
      sentence: 'Faxes from Denver to Utah numbers would cost about $0.40 less a month from your Salt Lake City trunk (estimate).',
      action: 'To send them from there, add a sending rule for numbers in Utah that sends from the Salt Lake City site (Providers → Rules).' };
    const advice: Advice = { days: 30, estimate: true, sentence: item.sentence, items: [item], caller_id: 'x',
      carriers: [{ account: 'sip', carrier: 'AnveoDirect', by_jurisdiction: true, sentence: 'AnveoDirect prices US calls by whether they stay within one state.' }],
      prices: [{ route: 'sip-anveo', carrier: 'AnveoDirect', rows: 208237, differ: 99117, source_url: 'https://www.anveo.com/anveodirect.standard.csv',
        read_on: '2026-10-08', imported_at: null }] };
    render(<SiteAdvice client={fakeClient({ 'GET /routing/recommendations/sites': advice })} canWrite />);
    const panel = await screen.findByTestId('site-advice');
    expect(within(panel).getByText(`${item.sentence} ${item.action}`)).toBeTruthy();
    expect(within(panel).getByText((208237).toLocaleString())).toBeTruthy();
    expect(within(panel).getByTestId('state-prices-import')).toBeTruthy();
  });
});

describe('Send: the check before a first fax', () => {
  it('warns when NPPES lists the number for someone else, and the fax still goes', async () => {
    const asked: string[] = [];
    let posted = false;
    server.use(
      http.get('/routing/recipient-check', ({ request }) => {
        asked.push(request.url);
        return HttpResponse.json({ number: '+13035550121', first_send: true, checked: true, state: 'listed_for_other', warning: true,
          sentence: 'This number is listed for SYNTHETIC HEALTH IMAGING LLC in NPPES, not Synthetic Health Clinic.', name: 'Synthetic Health Clinic',
          listed: [], source_url: 'https://npiregistry.cms.hhs.gov/api-page' });
      }),
      http.post('/fax', () => {
        posted = true;
        return HttpResponse.json({ id: 'f'.repeat(32), status: 'queued', delivery_state: 'ready' }, { status: 202 });
      }),
    );
    render(<SendFax client={client()} config={{ fax_disabled: false, max_file_size_mb: 10 }} configLoading={false} configError={null} />);
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+13035550121' } });
    fireEvent.change(screen.getByTestId('send-recipient-name'), { target: { value: 'Synthetic Health Clinic' } });
    fireEvent.blur(screen.getByTestId('send-recipient-name'));
    const warning = await screen.findByTestId('send-recipient-check', {}, { timeout: 3000 });
    expect(warning.textContent).toBe('This number is listed for SYNTHETIC HEALTH IMAGING LLC in NPPES, not Synthetic Health Clinic.');
    expect(asked.some((url) => url.includes('name=Synthetic+Health+Clinic') || url.includes('name=Synthetic%20Health%20Clinic'))).toBe(true);
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement,
      { target: { files: [new File(['%PDF-1.4 note'], 'note.pdf', { type: 'application/pdf' })] } });
    const send = screen.getByRole('button', { name: 'Send Fax' }) as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    fireEvent.click(send);
    await waitFor(() => expect(posted).toBe(true));
  });
});
