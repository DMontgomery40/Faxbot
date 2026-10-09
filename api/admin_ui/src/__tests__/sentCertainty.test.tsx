// Sent faxes Faxbot could not confirm: the checks cheapest first, and only a person's click sends or settles.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { CertaintyItem } from '../api/certaintyTypes';
import { FaxCertaintyItem, certaintyStateSentence } from '../components/work/SentCertainty';
import UncertainQueue, { uncertainSummary } from '../components/work/UncertainQueue';
import UncertainSettingsPanel from '../components/work/UncertainSettingsPanel';
import { shortTime } from '../components/work/text';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const item = (overrides: Partial<CertaintyItem> = {}): CertaintyItem => ({
  id: 'item-1', fax_id: 'fax-1', reference: 'K7Q4MC', state: 'open', state_key: 'assigned',
  state_text: 'Assigned to Dana; settle by 8 Oct 9:00 AM UTC.', why: 'The fax service did not confirm it accepted the fax.',
  category: 'transport_ambiguous', to_number: '********0123', pages: 3, sent_at: '2026-10-07T09:00:00', mailbox: null,
  owner: { id: 'dana', name: 'Dana' }, owner_source_text: 'the person who sent it', due_at: '2026-10-08T09:00:00',
  due_hours: 24, escalated_at: null, overdue: false, outcome: null, settled_by: null, settled_at: null,
  settled_reason: null, resend_fax_id: null, query_fax_id: null, is_mine: true, version: 1,
  actions: ['settle', 'send_query'], suggestion: null, number: '+12025550123', moved_on: null,
  checks: [
    { kind: 'partner', title: 'Ask the partner', cost: 'Free', automatic: true, result: 'unavailable', strength: null,
      text: 'This number is not one of your partners, so there is no partner to ask.', meaning: null, action: null },
    { kind: 'call_record', title: 'Read the call record', cost: 'Free', automatic: true,
      result: 'probably_not_delivered', strength: 'reading',
      text: "Telnyx's record of the call shows the call lasted about 12 seconds; even the first page needs about 24 seconds.",
      meaning: 'The fax probably did not arrive. This reads the call record; it is not proof.', action: null },
    { kind: 'receipt_query', title: 'Fax a receipt query', cost: 'One page', automatic: false, result: 'not_done',
      strength: null, text: 'A one-page fax asking the recipient to tick a box and fax it back.', meaning: null,
      action: 'send_query' },
    { kind: 'phone_call', title: 'Phone the recipient', cost: 'A few minutes', automatic: false, result: 'not_done',
      strength: null, text: 'Call the recipient and ask whether the fax arrived complete.', meaning: null, action: 'call',
      script: ['Call +12025550123.', 'Say: "This is Riverside Clinic, about the fax we sent on 7 Oct: 3 pages, reference K7Q4MC."'] },
  ],
  ...overrides,
});

describe('sent faxes to settle', () => {
  it('shows the time to settle in local time', () => {
    expect(certaintyStateSentence(item())).toBe(`Assigned to Dana; settle by ${shortTime('2026-10-08T09:00:00')}.`);
    expect(certaintyStateSentence(item({ state_key: 'settled', state_text: 'Settled as delivered by Dana.' })))
      .toBe('Settled as delivered by Dana.');
    expect(uncertainSummary({ open: 1, mine: 1, unassigned: 0, overdue: 0, settled: 0 })).toBe('1 sent fax needs settling.');
    expect(uncertainSummary({ open: 3, mine: 1, unassigned: 0, overdue: 2, settled: 0 }))
      .toBe('3 sent faxes need settling; 2 overdue.');
  });

  it('lists the checks cheapest first, sends nothing by itself, and settles only on a click', async () => {
    const posted: Array<{ path: string; body: unknown }> = [];
    server.use(
      http.get('/certainty/faxes/fax-1', () => HttpResponse.json({ items: [item({ suggestion: 'not_delivered' })], about: null })),
      http.post('/certainty/items/item-1/settle', async ({ request }) => {
        const body = await request.json();
        posted.push({ path: 'settle', body });
        return HttpResponse.json(item({ state: 'settled', state_key: 'settled', outcome: 'not_delivered',
          state_text: 'Settled as not delivered by Dana. The fax was sent again as a new fax.', resend_fax_id: 'fax-2',
          version: 2, actions: [], settled_reason: 'Front desk has no fax' }));
      }),
      http.post('/certainty/items/item-1/receipt-query', async ({ request }) => {
        posted.push({ path: 'query', body: await request.json() });
        return HttpResponse.json(item({ query_fax_id: 'fax-q', version: 2, actions: ['settle'] }));
      }),
    );
    const opened: string[] = [];
    render(<FaxCertaintyItem client={client()} jobId="fax-1" onOpenFax={(faxId) => opened.push(faxId)} />);
    expect(await screen.findByText('What happened to this fax?')).toBeTruthy();
    const titles = screen.getAllByText(/^\d\. /).map((node) => node.textContent);
    expect(titles).toEqual(['1. Ask the partner', '2. Read the call record', '3. Fax a receipt query', '4. Phone the recipient']);
    expect(screen.getByText('Probably not delivered')).toBeTruthy();
    expect(screen.getByText('The fax probably did not arrive. This reads the call record; it is not proof.')).toBeTruthy();
    expect(screen.getByText('Call +12025550123.')).toBeTruthy();
    expect(screen.getByText('The checks point to “Not delivered”. You decide.')).toBeTruthy();
    expect(posted).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Fax it to the recipient' }));
    await waitFor(() => expect(posted).toEqual([{ path: 'query', body: { version: 1 } }]));
    expect(await screen.findByText('Receipt query sent. When the recipient faxes it back, settle this fax.')).toBeTruthy();

    const settle = screen.getByRole('button', { name: 'Settle' }) as HTMLButtonElement;
    expect(settle.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('Not delivered'));
    fireEvent.click(screen.getByLabelText('Send it again now, as a new fax linked to this one'));
    fireEvent.change(screen.getByLabelText('How do you know?'), { target: { value: 'Front desk has no fax' } });
    fireEvent.click(screen.getByRole('button', { name: 'Settle' }));
    await waitFor(() => expect(posted[1]).toEqual({ path: 'settle', body: {
      outcome: 'not_delivered', reason: 'Front desk has no fax', version: 2, send_again: true } }));
    expect(await screen.findByText('Settled. The fax is on its way again as a new fax.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open the new fax' }));
    expect(opened).toEqual(['fax-2']);
  });

  it('says when the fax is already on its way again, and offers no second send', async () => {
    server.use(http.get('/certainty/faxes/fax-1', () => HttpResponse.json({ items: [item({ moved_on: { kind: 'resent',
      text: 'Faxbot is already sending this fax again by another route, so it is not sent again from here.' } })],
    about: null })));
    render(<FaxCertaintyItem client={client()} jobId="fax-1" />);
    expect(await screen.findByText('Faxbot is already sending this fax again by another route, so it is not sent again from here.')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Not delivered'));
    expect(screen.queryByLabelText('Send it again now, as a new fax linked to this one')).toBeNull();
  });

  it('asks to check the number for a fax a person answered, with the NPI registry and no receipt query', async () => {
    const answered = item({
      category: 'person_answered', why: 'A person answered at this number; check the fax number with the recipient.',
      actions: ['settle'], suggestion: 'not_delivered', checks: [
        { kind: 'call_record', title: 'Read the call record', cost: 'Free', automatic: true, result: 'not_delivered',
          strength: 'proof', text: 'A person answered the call, not a fax machine, so nothing arrived. Faxbot did not '
            + 'call the number again.', meaning: 'Settle it as not delivered, and send it again only to the right fax '
            + 'number.', action: null },
        { kind: 'phone_call', title: 'Phone the recipient', cost: 'A few minutes', automatic: false, result: 'not_done',
          strength: null, text: 'Call the recipient and ask for the right fax number.', meaning: null, action: 'call',
          script: ['Call the recipient. The number Faxbot faxed, +12025550123, may be a voice line.'] },
        { kind: 'npi_lookup', title: 'Look the provider up in the NPI registry', cost: 'Free', automatic: false,
          result: 'not_done', strength: null, text: 'Look the provider up in the NPI registry.', meaning: null,
          action: null, source_url: 'https://npiregistry.cms.hhs.gov/api-page' },
      ] });
    server.use(http.get('/certainty/faxes/fax-1', () => HttpResponse.json({ items: [answered], about: null })));
    render(<FaxCertaintyItem client={client()} jobId="fax-1" />);
    expect(await screen.findByText('A person answered at this number; check the fax number with the recipient.'))
      .toBeTruthy();
    expect(screen.getByText('Call the recipient. The number Faxbot faxed, +12025550123, may be a voice line.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'The NPI registry' }).getAttribute('href'))
      .toBe('https://npiregistry.cms.hhs.gov/api-page');
    expect(screen.queryByRole('button', { name: 'Fax it to the recipient' })).toBeNull();
  });

  it('says a fax sent again points back to the earlier one', async () => {
    server.use(http.get('/certainty/faxes/fax-2', () => HttpResponse.json({ items: [], about: { fax_id: 'fax-1', kind: 'resend' } })));
    const opened: string[] = [];
    render(<FaxCertaintyItem client={client()} jobId="fax-2" onOpenFax={(faxId) => opened.push(faxId)} />);
    expect(await screen.findByText('This fax was sent again because an earlier fax did not arrive.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open it' }));
    expect(opened).toEqual(['fax-1']);
  });

  it('shows nothing for a fax Faxbot is sure of', async () => {
    const { container } = render(<FaxCertaintyItem client={client()} jobId="fax-9" />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container.textContent).toBe('');
  });

  it('lists sent faxes to settle beside the received work and opens one in Sent', async () => {
    server.use(
      http.get('/certainty/counts', () => HttpResponse.json({ open: 1, mine: 1, unassigned: 0, overdue: 0, settled: 0 })),
      http.get('/certainty/items', () => HttpResponse.json({ items: [item({ checks: undefined })] })),
    );
    const opened: string[] = [];
    render(<UncertainQueue client={client()} onOpenSentFax={(faxId) => opened.push(faxId)} />);
    expect(await screen.findByText('Sent faxes to settle')).toBeTruthy();
    expect(screen.getByText(/1 sent fax needs settling\./)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    expect(opened).toEqual(['fax-1']);
  });

  it('saves the hours to settle and the fallback person', async () => {
    let saved: unknown = null;
    server.use(
      http.get('/certainty/settings', () => HttpResponse.json({ settle_hours: 24, version: 0, fallback: null,
        people: [{ id: 'fran', name: 'Fran', login: 'fran' }] })),
      http.put('/certainty/settings', async ({ request }) => {
        saved = await request.json();
        return HttpResponse.json({ settle_hours: 8, version: 1, fallback: { id: 'fran', name: 'Fran' },
          people: [{ id: 'fran', name: 'Fran', login: 'fran' }] });
      }),
    );
    render(<UncertainSettingsPanel client={client()} canWrite />);
    const hours = await screen.findByLabelText('Settle within (hours)');
    fireEvent.change(hours, { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(saved).toEqual({ fallback_principal_id: null, settle_hours: 8, version: 0 }));
    expect(await screen.findByText('Saved. Faxes Faxbot finds uncertain from now on use these settings.')).toBeTruthy();
  });
});
