// Costs → Recommendations → fax lines in a POTS-replacement order (N23). Synthetic data only.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import PotsCounterQuote, { type PotsView } from '../components/delivery/PotsCounterQuote';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const AIRDIAL = { id: 'ooma-airdial', name: 'Ooma AirDial', per_line: '39.95',
  sentence: 'Ooma AirDial at $39.95 a line a month, including the device, its wireless data and phone service.',
  source: 'https://touchtone.net/services/ooma/Ooma-AirDial-is-Now-Availabe-Through-TouchTone.pdf',
  label: "TouchTone's Ooma AirDial partner sheet, revision of 25 August 2023", read_on: '2026-10-10' };
const EMPTY: PotsView = { quotes: [], published: [AIRDIAL], note: 'Faxbot only advises: it never orders, ports or cancels a line.',
  sentence: 'No POTS-replacement quote yet. Enter the price per line from the quote, or start from a published price.' };
const QUOTED: PotsView = { ...EMPTY, sentence: null, quotes: [{ name: 'Ooma AirDial', per_line: '$39.95', source_url: AIRDIAL.source,
  source_date: '2023-08-25', sentences: ['Your inventory has 7 lines: 5 fax, 1 alarm, elevator or emergency, and 1 other or not known.',
    'Take the 5 fax lines out of the Ooma AirDial order: at $39.95 a line a month, that is $199.75 a month.',
    "Faxbot can send and receive them on one shared trunk instead: at Telnyx's published prices, 5 numbers at $1.00 a month and no trunk fee, so $5.00 a month.",
    'Keep the 1 alarm, elevator and emergency line in the order.'] }] };

describe('Fax lines in a POTS-replacement order', () => {
  it('starts from the published price and shows the counter-quote', async () => {
    const sent: unknown[] = [];
    server.use(http.get('/routing/pots-quotes', () => HttpResponse.json(EMPTY)),
      http.put('/routing/pots-quotes', async ({ request }) => { sent.push(await request.json()); return HttpResponse.json(QUOTED); }));
    render(<PotsCounterQuote client={client()} canWrite />);
    const section = await screen.findByTestId('pots-counter-quote');
    expect(section.textContent).toContain('No POTS-replacement quote yet.');
    fireEvent.click(screen.getByRole('button', { name: 'Enter a quote' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('link', { name: /TouchTone/ }).getAttribute('href')).toBe(AIRDIAL.source);
    fireEvent.change(within(dialog).getByLabelText('Term in months'), { target: { value: '36' } });
    fireEvent.click(within(dialog).getByRole('button', { name: "Use Ooma AirDial's published price" }));
    await waitFor(() => expect(sent).toEqual([{ published: 'ooma-airdial', term_months: 36, lines_quoted: null }]));
    await waitFor(() => expect(section.textContent).toContain('that is $199.75 a month'));
    expect(section.textContent).toContain("at Telnyx's published prices");
  });

  it('records a quote you enter and lets you withdraw it', async () => {
    const sent: unknown[] = [];
    const withdrawn: unknown[] = [];
    server.use(http.get('/routing/pots-quotes', () => HttpResponse.json(QUOTED)),
      http.put('/routing/pots-quotes', async ({ request }) => { sent.push(await request.json()); return HttpResponse.json(QUOTED); }),
      http.post('/routing/pots-quotes/remove', async ({ request }) => { withdrawn.push(await request.json()); return HttpResponse.json(EMPTY); }));
    render(<PotsCounterQuote client={client()} canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: 'Enter a quote' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Product quoted'), { target: { value: 'Box' } });
    fireEvent.change(within(dialog).getByLabelText('Price per line a month'), { target: { value: '45' } });
    fireEvent.change(within(dialog).getByLabelText('Analog ports on one device'), { target: { value: '8' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(sent).toEqual([{ name: 'Box', per_line: '45', currency: 'USD', term_months: null, lines_quoted: null,
      ports_per_device: 8, device_price: '', source_url: null, source_date: null }]));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw the Ooma AirDial quote' }));
    await waitFor(() => expect(withdrawn).toEqual([{ name: 'Ooma AirDial' }]));
  });

  it('gives readers the counter-quote without buttons', async () => {
    server.use(http.get('/routing/pots-quotes', () => HttpResponse.json(QUOTED)));
    let counted: number | null = null;
    render(<PotsCounterQuote client={client()} canWrite={false} onCount={(count) => { counted = count; }} />);
    expect((await screen.findByTestId('pots-counter-quote')).textContent).toContain('Keep the 1 alarm');
    expect(screen.queryByRole('button', { name: 'Enter a quote' })).toBeNull();
    await waitFor(() => expect(counted).toBe(1));
  });
});
