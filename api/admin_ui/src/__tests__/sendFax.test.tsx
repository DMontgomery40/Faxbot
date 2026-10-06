// A send keeps its Idempotency-Key across a reload, so retrying after a lost
// answer cannot send the fax twice.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import SendFax from '../components/SendFax';
import { PENDING_SEND_LIFETIME_MS } from '../components/sendIntent';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const config = { fax_disabled: false, max_file_size_mb: 10 };

const document = (lastModified = 1_790_000_000_000, text = '%PDF-1.4 referral') =>
  new File([text], 'referral.pdf', { type: 'application/pdf', lastModified });

type Answer = 'lost' | 'accepted';

function faxServer(answers: Answer[]) {
  const keys: string[] = [];
  server.use(http.post('/fax', ({ request }) => {
    keys.push(request.headers.get('Idempotency-Key') ?? '');
    const answer = answers.shift() ?? 'accepted';
    if (answer === 'lost') return HttpResponse.error();
    return HttpResponse.json({ id: 'b'.repeat(32), status: 'queued', delivery_state: 'ready' }, { status: 202 });
  }));
  return keys;
}

function openSend() {
  return render(<SendFax client={client()} config={config} configLoading={false} configError={null} />);
}

async function send(number: string, file: File) {
  fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: number } });
  fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [file] } });
  fireEvent.click(screen.getByRole('button', { name: 'Send Fax' }));
}

const lostAnswer = /^Couldn't reach the server, so the fax may or may not have been submitted\. To retry without creating a duplicate, send the same document to the same number again, even after reloading this page\./;

describe('Send keeps the send identity across a reload', () => {
  beforeEach(() => window.sessionStorage.clear());

  it('retries with the same key after a reload during a send, then forgets it once accepted', async () => {
    const keys = faxServer(['lost', 'accepted']);
    const first = openSend();
    await send('+15551234567', document());
    expect(await screen.findByText(lostAnswer)).toBeTruthy();
    first.unmount(); // the page is reloaded

    openSend();
    await send('+1 (555) 123-4567', document());
    expect(await screen.findByText('Fax queued for sending.')).toBeTruthy();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
    expect(window.sessionStorage.getItem('faxbot_pending_send')).toBeNull();
  });

  it('says it is resuming the earlier send', async () => {
    faxServer(['lost']);
    const first = openSend();
    await send('+15551234567', document());
    await screen.findByText(lostAnswer);
    first.unmount();
    let release: () => void = () => undefined;
    server.use(http.post('/fax', async () => {
      await new Promise<void>((resolve) => { release = resolve; });
      return HttpResponse.json({ id: 'b'.repeat(32), status: 'queued' }, { status: 202 });
    }));
    openSend();
    await send('+15551234567', document());
    expect(await screen.findByText('Resuming your earlier send.')).toBeTruthy();
    release();
    expect(await screen.findByText('Fax queued for sending.')).toBeTruthy();
    expect(screen.queryByText('Resuming your earlier send.')).toBeNull();
  });

  it('uses a new key for a different document or number', async () => {
    const keys = faxServer(['lost', 'lost', 'lost']);
    const first = openSend();
    await send('+15551234567', document());
    await screen.findByText(lostAnswer);
    first.unmount();

    const second = openSend();
    await send('+15551234567', document(1_790_000_000_001));
    await screen.findByText(lostAnswer);
    second.unmount();

    openSend();
    await send('+15557654321', document(1_790_000_000_001));
    await screen.findByText(lostAnswer);
    expect(new Set(keys).size).toBe(3);
  });

  it('gives a deliberate new send after success a new key, even for the same document and number', async () => {
    const keys = faxServer(['accepted', 'accepted']);
    openSend();
    await send('+15551234567', document());
    await screen.findByText('Fax queued for sending.');
    await send('+15551234567', document());
    await vi.waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).not.toBe(keys[0]);
  });

  it('forgets an earlier send after a day', async () => {
    const keys = faxServer(['lost', 'accepted']);
    const first = openSend();
    await send('+15551234567', document());
    await screen.findByText(lostAnswer);
    first.unmount();
    const stored = JSON.parse(window.sessionStorage.getItem('faxbot_pending_send')!);
    stored.createdAt -= PENDING_SEND_LIFETIME_MS + 1000;
    window.sessionStorage.setItem('faxbot_pending_send', JSON.stringify(stored));

    openSend();
    await send('+15551234567', document());
    await screen.findByText('Fax queued for sending.');
    expect(keys[1]).not.toBe(keys[0]);
  });

  it('forgets the earlier send when the form is cleared', async () => {
    const keys = faxServer(['lost', 'accepted']);
    openSend();
    await send('+15551234567', document());
    await screen.findByText(lostAnswer);
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(window.sessionStorage.getItem('faxbot_pending_send')).toBeNull();
    await send('+15551234567', document());
    await screen.findByText('Fax queued for sending.');
    expect(keys[1]).not.toBe(keys[0]);
  });

  it('never stores the number or file name', async () => {
    faxServer(['lost']);
    openSend();
    await send('+15551234567', document());
    await screen.findByText(lostAnswer);
    const stored = window.sessionStorage.getItem('faxbot_pending_send') ?? '';
    expect(stored).not.toContain('5551234567');
    expect(stored).not.toContain('referral');
  });

  it('still sends when the browser refuses session storage', async () => {
    const keys = faxServer(['accepted']);
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
    try {
      openSend();
      await send('+15551234567', document());
      expect(await screen.findByText('Fax queued for sending.')).toBeTruthy();
      expect(keys).toHaveLength(1);
    } finally {
      setItem.mockRestore();
      getItem.mockRestore();
    }
  });
});

describe('Send follows the installation country', () => {
  beforeEach(() => window.sessionStorage.clear());

  const gb = { ...config, number_format: { country: 'GB', national: '0121 234 5678', international: '' } };
  const us = { ...config, number_format: { country: 'US', national: '(201) 555-0123', international: '' } };
  const openFor = (settings: typeof gb) =>
    render(<SendFax client={client()} config={settings} configLoading={false} configError={null} />);

  it('shows the UK example for a UK installation and sends a number typed the UK way', async () => {
    // jsdom's form data does not reach the test server, so read what the console adds to it.
    const append = vi.spyOn(FormData.prototype, 'append');
    const sent = () => append.mock.calls.filter(([name]) => name === 'to').map(([, value]) => value);
    server.use(http.post('/fax', () => {
      return HttpResponse.json({ id: 'c'.repeat(32), status: 'queued', delivery_state: 'ready', to: '+441782684953' },
        { status: 202 });
    }));
    openFor(gb);
    expect(screen.getByText('For example 0121 234 5678, or a number starting with + and its country code.')).toBeTruthy();
    expect(screen.getByPlaceholderText('0121 234 5678')).toBeTruthy();
    expect(screen.getByText(/Numbers without a country code are read as United Kingdom numbers, for example 0121 234 5678/))
      .toBeTruthy();
    expect(screen.queryByText(/555/)).toBeNull();

    try {
      await send('01782 684953', document());
      // The confirmation names the number the server stored.
      expect(await screen.findByText('Fax queued for +441782684953.')).toBeTruthy();
      expect(sent()).toEqual(['01782684953']);
    } finally {
      append.mockRestore();
    }
  });

  it('still shows the US example for a US installation', () => {
    openFor(us);
    expect(screen.getByText('For example (201) 555-0123, or a number starting with + and its country code.')).toBeTruthy();
    expect(screen.getByPlaceholderText('(201) 555-0123')).toBeTruthy();
    expect(screen.getByText(/read as United States numbers, for example \(201\) 555-0123/)).toBeTruthy();
  });

  it('shows the server sentence when it cannot read the number, and keeps no send to resume', async () => {
    const detail = 'Enter the full fax number with its area code, or with its country code starting with +.';
    server.use(http.post('/fax', () => HttpResponse.json({ detail }, { status: 400 })));
    openFor(gb);
    await send('684953', document());
    expect(await screen.findByText(detail)).toBeTruthy();
    expect(screen.queryByText(/To retry without creating a duplicate/)).toBeNull();
    expect(screen.queryByText(/HTTP/)).toBeNull();
    expect(window.sessionStorage.getItem('faxbot_pending_send')).toBeNull();
  });

  it('confirms with the number and a way to follow the fax in Jobs, never a raw job ID', async () => {
    const id = 'c'.repeat(32);
    server.use(http.post('/fax', () => HttpResponse.json({ id, to: '+12015550123', status: 'queued',
      delivery_state: 'ready' }, { status: 202 })));
    const opened = vi.fn();
    render(<SendFax client={client()} config={us} configLoading={false} configError={null} onOpenJob={opened} />);
    await send('2015550123', document());
    expect(await screen.findByText('Fax queued for +12015550123.')).toBeTruthy();
    expect(screen.queryByText(/Job ID|cccccccc/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'See it in Sent' }));
    expect(opened).toHaveBeenCalledWith(id);
  });

  it('says why a fax was refused when Faxbot cannot sign in to its fax engine', async () => {
    const detail = "Faxbot can't sign in to its fax engine. Check that the Asterisk manager password matches.";
    server.use(http.post('/fax', () => HttpResponse.json({ detail }, { status: 503 })));
    openFor(us);
    await send('2015550123', document());
    expect(await screen.findByText(detail)).toBeTruthy();
    expect(screen.queryByText(/HTTP|Check Jobs|Check Sent/)).toBeNull();
    expect(window.sessionStorage.getItem('faxbot_pending_send')).toBeNull();
  });
});

describe('Before sending', () => {
  it('words a per-minute and a per-page route by their own units, and estimates this document once its pages are known', async () => {
    const asked: string[] = [];
    let card = { label: 'Telnyx', rate: '$0.005 a minute, at least 1 minute', one: '0.005', two: '0.01' };
    server.use(http.get('/routing/destinations/:number', ({ params, request }) => {
      const pages = Number(new URL(request.url).searchParams.get('pages') ?? '1');
      asked.push(`${params.number} ${pages}`);
      return HttpResponse.json({ number: params.number, display_name: null, notes: null, preferred_route: null,
        accepts_references: false, version: 0, routes: [], estimated_cost_30_days: [], direct_partner: null, available_routes: [],
        recommended_routes: [{ route: 'sip', label: card.label, reason: 'cheapest',
          explanation: 'Faxbot picks the cheapest route that works reliably.',
          estimated_cost_one_page: { currency: 'USD', amount: card.one }, pages,
          estimated_cost: { currency: 'USD', amount: pages === 2 ? card.two : card.one }, rate: card.rate,
          included_in_plan: false, monthly_fee: null }] });
    }));
    const view = openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    const route = await screen.findByTestId('send-route', {}, { timeout: 2000 });
    expect(route.textContent).toContain('Faxbot will send it through Telnyx.');
    expect(route.textContent).toContain('Faxbot picks the cheapest route that works reliably.');
    // Without a document, the price in the carrier's own unit: never "a page" for a per-minute carrier.
    expect(screen.getByTestId('send-cost').textContent).toBe('About $0.005 a minute, at least 1 minute.');
    const twoPages = new File(['%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Type /Page >> endobj\n'
      + '3 0 obj << /Type /Pages /Count 2 >> endobj\n'], 'two.pdf', { type: 'application/pdf' });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages] } });
    await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
      .toBe('About $0.01 for this 2-page fax ($0.005 a minute, at least 1 minute).'), { timeout: 2000 });
    expect(asked).toEqual(['+12025550123 1', '+12025550123 2']);
    view.unmount();
    card = { label: 'Phaxio', rate: '$0.07 a page', one: '0.07', two: '0.14' };
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages] } });
    await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
      .toBe('About $0.14 for this 2-page fax ($0.07 a page).'), { timeout: 2000 });
    expect(screen.getByRole('heading', { name: 'Send a fax' })).toBeTruthy();
  });

  it('offers a real call for one of your own numbers and sends that choice', async () => {
    server.use(http.get('/routing/destinations/:number', ({ params }) => HttpResponse.json({ number: params.number,
      display_name: null, notes: null, preferred_route: null, accepts_references: false, version: 0, routes: [],
      estimated_cost_30_days: [], direct_partner: null, available_routes: [],
      recommended_routes: [{ route: 'local', label: 'This Faxbot', reason: 'own_number',
        explanation: '+1 202-555-0123 is one of your own fax numbers, so the fax goes straight into Received without a phone call.',
        estimated_cost_one_page: null, rate: null, included_in_plan: false, monthly_fee: null }] })));
    faxServer(['accepted']);
    const appended = vi.spyOn(FormData.prototype, 'append');
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    const choice = await screen.findByTestId('send-by-call', {}, { timeout: 2000 });
    expect(screen.getByTestId('send-route').textContent).toContain('goes straight into Received');
    fireEvent.click(choice.querySelector('input') as HTMLInputElement);
    await send('+12025550123', document());
    await waitFor(() => expect(appended.mock.calls.some(([name, value]) => name === 'send_by_call' && String(value) === 'true')).toBe(true));
    appended.mockRestore();
  });

  it('sends without a route line when the number has no recommendation or routing is not readable', async () => {
    server.use(http.get('/routing/destinations/:number', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(screen.queryByTestId('send-route')).toBeNull();
    expect(screen.queryByText(/Forbidden|couldn't/)).toBeNull();
  });
});
