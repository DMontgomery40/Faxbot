// Costs → Charges and Costs → Invoices. Synthetic data only; every request is answered by the fake server.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import Charges, { lastChecked } from '../components/delivery/Charges';
import Invoices, { lastMonth } from '../components/delivery/Invoices';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const usd = (amount: string) => ({ currency: 'USD', amount });

const charges = {
  since: '2026-09-08T00:00:00',
  accounts: [
    { account_key: 'sinch', provider_id: 'sinch', label: 'Sinch', listing: true, last_checked: '2026-10-08T09:00:00',
      last_outcome: 'complete', sentence: 'Sinch reports what each sent and received fax cost; Faxbot reads it after each fax.' },
    { account_key: 'humblefax', provider_id: 'humblefax', label: 'HumbleFax', listing: false, last_checked: null,
      last_outcome: null, sentence: 'HumbleFax reports no charge per fax. Enter its monthly invoice under Costs → Invoices.' },
  ],
  received: [{ provider_id: 'sinch', label: 'Sinch', faxes: 3, charged: 2, waiting: 1, never_priced: 0, cost: [usd('0.14')],
    summary: 'Sinch charged $0.14 for 2 received faxes; 1 is waiting for Sinch\'s price.' }],
  unrecorded: [{ id: 'u-1', account_key: 'sinch', provider_id: 'sinch', label: 'Sinch', direction: 'sent',
    from_number: null, to_number: '+12025550123', time: '2026-10-03T14:22:05', pages: 1, cost: usd('0.045'),
    may_be_uncertain_fax: null, summary: "Sinch billed a fax to +12025550123 on 3 Oct ($0.045) that Faxbot didn't send." }],
  trunk: { preset: 'gamma', published: false, readable: false, sources: [],
    sentence: 'Gamma publishes no call records with charges that Faxbot can read; enter its invoice under Costs → Invoices.' },
};

describe('Costs → Charges', () => {
  it('shows how each account is read and the faxes a provider billed that Faxbot has no record of', async () => {
    const sweeps: unknown[] = [];
    server.use(
      http.get('/routing/charges', () => HttpResponse.json(charges)),
      http.post('/routing/charges/sweep', async ({ request }) => {
        sweeps.push(await request.json());
        return HttpResponse.json({ results: [], summary: 'Sinch listed 4 faxes; Faxbot has no record of one of them.' });
      }),
    );
    render(<Charges client={client()} canWrite />);
    expect(await screen.findByText(charges.accounts[1].sentence)).toBeTruthy();
    expect(screen.getByText(charges.received[0].summary)).toBeTruthy();
    const row = screen.getByTestId('unrecorded-fax');
    expect(within(row).getByText(charges.unrecorded[0].summary)).toBeTruthy();
    expect(within(row).getByText('$0.045')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Check now' }));
    expect(await screen.findByText('Sinch listed 4 faxes; Faxbot has no record of one of them.')).toBeTruthy();
    expect(sweeps).toEqual([{ account: null, days: 7 }]);
  });

  it('offers no check to someone who can only read, and says when nothing is missing', async () => {
    server.use(http.get('/routing/charges', () => HttpResponse.json({ ...charges, unrecorded: [] })));
    render(<Charges client={client()} canWrite={false} />);
    expect(await screen.findByText('Your providers listed no fax that Faxbot has no record of.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Check now' })).toBeNull();
  });

  it('says when an account was not checked yet or could not be reached', () => {
    const [sinch, humblefax] = charges.accounts as Parameters<typeof lastChecked>[0][];
    expect(lastChecked(humblefax)).toBe('-');
    expect(lastChecked({ ...sinch, last_checked: null })).toBe('Not checked yet');
    expect(lastChecked({ ...sinch, last_outcome: 'unavailable' })).toMatch(/: Could not be reached$/);
  });
});

const invoice = {
  id: 'inv-1', account_key: 'humblefax', provider_id: 'humblefax', label: 'HumbleFax', first_day: '2026-09-01',
  last_day: '2026-09-30', period_name: 'September 2026', total: usd('13.20'), explained: usd('10.00'),
  residual: usd('3.20'), state: 'residual', complete: true,
  summary: "$3.20 of your HumbleFax invoice for September 2026 isn't explained by your faxes.",
  parts: [{ label: 'Plan fee', amount: usd('10.00'), kind: 'plan' }],
  notes: ['5 faxes included in the plan at no extra charge.'], faxes: { sent: 4, received: 1, calls: 0, not_priced: 0 },
  note: 'INV-0042', file: { name: 'invoice.pdf', type: 'application/pdf', size: 40 }, version: 1,
  entered_by: 'Synthetic Admin', entered_at: '2026-10-02T09:00:00',
};
const accounts = [
  { account_key: 'humblefax', provider_id: 'humblefax', label: 'HumbleFax', currency: 'USD', billing_day: 1 },
  { account_key: 'sinch-uk', provider_id: 'sinch', label: 'Sinch UK', currency: 'GBP', billing_day: 15 },
];

describe('Costs → Invoices', () => {
  it('lists each invoice with the part your faxes do not explain, and the recommendation when it recurs', async () => {
    const advice = 'Your HumbleFax invoices were more than your faxes explain in July 2026 and September 2026 ($3.10 and $3.20).';
    server.use(
      http.get('/routing/invoices', () => HttpResponse.json({ invoices: [invoice], accounts,
        recommendations: [{ account_key: 'humblefax', direction: 'more', invoices: ['inv-0', 'inv-1'], text: advice }] })),
      http.get('/routing/invoices/inv-1', () => HttpResponse.json({ ...invoice, current: true, history: [
        { id: 'inv-1', version: 1, total: usd('13.20'), entered_by: 'Synthetic Admin', entered_at: '2026-10-02T09:00:00', note: null },
      ] })),
    );
    render(<Invoices client={client()} canWrite />);
    expect(await screen.findByText(advice)).toBeTruthy();
    const row = screen.getByTestId('invoice-row');
    expect(within(row).getByText('$13.20')).toBeTruthy();
    expect(within(row).getByText('$3.20')).toBeTruthy();
    fireEvent.click(within(row).getByRole('button', { name: 'Details of the HumbleFax invoice for September 2026' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(invoice.summary)).toBeTruthy();
    expect(within(dialog).getByText('Plan fee')).toBeTruthy();
    expect(within(dialog).getByText('5 faxes included in the plan at no extra charge.')).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: 'Download the invoice file' })).toBeTruthy();
  });

  it('enters an invoice with its file in the account currency and shows what it found', async () => {
    // jsdom's FormData does not travel through the test fetch, so what the client appends is checked.
    const appended = vi.spyOn(FormData.prototype, 'append');
    server.use(
      http.get('/routing/invoices', () => HttpResponse.json({ invoices: [], accounts, recommendations: [] })),
      http.post('/routing/invoices', () => HttpResponse.json({ ...invoice, account_key: 'sinch-uk', label: 'Sinch UK',
        summary: 'Your faxes explain all of your Sinch UK invoice for September 2026.' }, { status: 201 })),
    );
    render(<Invoices client={client()} canWrite />);
    expect(await screen.findByText(/No invoices yet/)).toBeTruthy();
    fireEvent.mouseDown(screen.getByLabelText('Account'));
    fireEvent.click(await screen.findByRole('option', { name: 'Sinch UK' }));
    expect(await screen.findByText("The month counts from the 15th, the plan's billing day.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Month'), { target: { value: '2026-09' } });
    fireEvent.change(screen.getByLabelText('Invoice total'), { target: { value: ' 41.07 ' } });
    fireEvent.change(screen.getByLabelText('Invoice file'), {
      target: { files: [new File(['%PDF-1.4'], 'september.pdf', { type: 'application/pdf' })] } });
    fireEvent.click(screen.getByRole('button', { name: 'Save the invoice' }));
    expect(await screen.findByText('Your faxes explain all of your Sinch UK invoice for September 2026.')).toBeTruthy();
    const sent = Object.fromEntries(appended.mock.calls.map(([name, value]) => [name,
      typeof value === 'string' ? value : (value as File).name]));
    expect(sent).toEqual({ account: 'sinch-uk', total: '41.07', currency: 'GBP', month: '2026-09', file: 'september.pdf' });
    appended.mockRestore();
  });

  it('shows no entry form to someone who can only read', async () => {
    server.use(http.get('/routing/invoices', () => HttpResponse.json({ invoices: [invoice], accounts, recommendations: [] })));
    render(<Invoices client={client()} canWrite={false} />);
    expect(await screen.findByTestId('invoice-row')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save the invoice' })).toBeNull();
  });

  it('starts the month field on last month', () => {
    expect(lastMonth(new Date(2026, 0, 15))).toBe('2025-12');
    expect(lastMonth(new Date(2026, 9, 8))).toBe('2026-09');
  });
});
