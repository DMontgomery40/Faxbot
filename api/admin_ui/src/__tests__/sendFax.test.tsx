// A send keeps its Idempotency-Key across a reload, so retrying after a lost
// answer cannot send the fax twice.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient, { RETRY_MISMATCH } from '../api/client';
import SendFax, { ENGINE_CHOICES } from '../components/SendFax';
import { PENDING_SEND_LIFETIME_MS } from '../components/sendIntent';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const config = { fax_disabled: false, max_file_size_mb: 10 };

// Send asks whether a header notice applies; none does here, so the cover sheet choice stays hidden.
beforeEach(() => {
  server.use(http.get('/header-notice/for-send', () => HttpResponse.json({ notice: null })));
});

const document = (lastModified = 1_790_000_000_000, text = '%PDF-1.4 referral') =>
  new File([text], 'referral.pdf', { type: 'application/pdf', lastModified });

it('preserves the selected PDF bytes when the request is encoded as multipart', async () => {
  const bytes = new Uint8Array([...new TextEncoder().encode('%PDF-1.4 referral\n'), 0, 128, 255]);
  const file = new File([bytes], 'referral.pdf', { type: 'application/pdf' });
  const form = new FormData();
  form.append('to', '+12025550123');
  form.append('file', file);
  // Use the same Request encoder as fetch/MSW, and actually consume its body.
  const request = new Request('https://synthetic.invalid/fax', { method: 'POST', body: form });
  expect(request.headers.get('Content-Type')).toMatch(/^multipart\/form-data; boundary=/);
  const decoded = await request.formData();
  const uploaded = decoded.get('file') as File;
  expect(decoded.get('to')).toBe('+12025550123');
  expect(uploaded.name).toBe('referral.pdf');
  expect(uploaded.type).toBe('application/pdf');
  expect(new Uint8Array(await uploaded.arrayBuffer())).toEqual(bytes);
});

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

const twoPages = () => new File(['%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Type /Page >> endobj\n'
  + '3 0 obj << /Type /Pages /Count 2 >> endobj\n'], 'two.pdf', { type: 'application/pdf' });

// The number's recommended route: Telnyx, priced by the minute.
const destinationAnswer = () => http.get('/routing/destinations/:number', ({ params }) => HttpResponse.json({
  number: params.number, display_name: null, notes: null, preferred_route: null, accepts_references: false, version: 0,
  routes: [], estimated_cost_30_days: [], direct_partner: null, available_routes: [],
  recommended_routes: [{ route: 'sip', label: 'Telnyx', reason: 'cheapest', explanation: 'Cheapest.',
    estimated_cost_one_page: { currency: 'USD', amount: '0.005' }, pages: 2,
    estimated_cost: { currency: 'USD', amount: '0.01' }, rate: '$0.005 a minute, at least 1 minute',
    included_in_plan: false, monthly_fee: null }] }));

const routeAnswer = (amount: string, headline: string, basis: string) => ({
  route: 'sip', label: 'Telnyx', billed_pages: 0, seconds: 46, billed_seconds: 60, seconds_to_next_step: 14,
  cost: { currency: 'USD', amount }, cost_text: null, marginal: false, headline, basis });

// The price from the page count alone (GET /routing/predict).
const pageCountPrediction = () => http.get('/routing/predict', () => HttpResponse.json({
  to: '+12025550123', number_class: 'local', number_class_text: 'a local number', pages: 2, layout: 'normal',
  resolution: 'fine', sentence: '', note: '',
  routes: [routeAnswer('0.01', 'About $0.01 for this 2-page fax.', 'Typical pages.')] }));

// The price of the document itself (POST /routing/predict), with the coding measured on its pages.
const documentAnswer = (to: string, amount: string) => ({
  to, number_class: 'local', number_class_text: 'a local number', pages: 2, layout: 'normal', resolution: 'fine',
  sentence: '', note: '', measured: { MH: 500000, MR: 288000, MMR: 432000 }, measured_sentence: null,
  jbig_measured: false,
  routes: [{ ...routeAnswer(amount, `About $${amount} for this measured 2-page fax.`, 'Measured on these pages.'),
    finish_sentence: 'From 4 earlier faxes to this number, 9 in 10 calls like this one finish within about 50 seconds.',
    coding: { coding: 'MR', measured: true,
      sentence: 'Faxbot would send these pages with MR: 33% shorter than MMR for these pages.' } }] });

const lostAnswer =/^Couldn't reach the server, so the fax may or may not have been submitted\. To retry without creating a duplicate, send the same document to the same number again, even after reloading this page\./;

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

  it("says in plain words when a retry doesn't match the fax sent before, and links Sent", async () => {
    server.use(http.post('/fax', () => HttpResponse.json(
      { detail: 'Idempotency-Key already belongs to a different fax request.' }, { status: 409 })));
    openSend();
    await send('+15551234567', document());
    expect(await screen.findByText(RETRY_MISMATCH)).toBeTruthy();
    expect(screen.queryByText(/Idempotency|409|To retry without creating a duplicate/)).toBeNull();
    expect(screen.getByTestId('send-open-sent')).toBeTruthy();
  });
});

// D24: the recipient name typed on Send belongs to that fax; Sent and the board show it.
describe('The recipient name typed on Send', () => {
  beforeEach(() => window.sessionStorage.clear());

  it('goes with the fax, and the next fax starts without it', async () => {
    const names: (FormDataEntryValue | null)[] = [];
    server.use(http.post('/fax', async ({ request }) => {
      names.push((await request.formData()).get('recipient_name'));
      return HttpResponse.json({ id: 'b'.repeat(32), status: 'queued', delivery_state: 'ready' }, { status: 202 });
    }));
    openSend();
    const name = screen.getByTestId('send-recipient-name') as HTMLInputElement;
    fireEvent.change(name, { target: { value: '  Faxbeep US test line ' } });
    await send('+15551234567', document());
    expect(await screen.findByText('Fax queued for sending.')).toBeTruthy();
    expect(names).toEqual(['Faxbeep US test line']);
    expect(name.value).toBe('');
    await send('+15551234568', document(1_790_000_000_002));
    await waitFor(() => expect(names).toHaveLength(2));
    expect(names[1]).toBeNull();
  });

  it('is cleared by Clear, so the next fax and its number check go without it', async () => {
    const names: (FormDataEntryValue | null)[] = [];
    const checked: Array<[string | null, string | null]> = [];
    server.use(http.post('/fax', async ({ request }) => {
      names.push((await request.formData()).get('recipient_name'));
      return HttpResponse.json({ id: 'b'.repeat(32), status: 'queued', delivery_state: 'ready' }, { status: 202 });
    }), http.get('/routing/recipient-check', ({ request }) => {
      const query = new URL(request.url).searchParams;
      checked.push([query.get('to'), query.get('name')]);
      return HttpResponse.json({ number: query.get('to'), first_send: true, checked: false, state: 'no_name',
        warning: false, sentence: null, name: null, listed: [], source_url: 'https://npiregistry.cms.hhs.gov/api-page' });
    }));
    openSend();
    const number = screen.getByRole('textbox', { name: /Destination Number/ });
    const name = screen.getByTestId('send-recipient-name') as HTMLInputElement;
    fireEvent.change(number, { target: { value: '+15551234567' } });
    fireEvent.change(name, { target: { value: 'Abandoned recipient' } });
    fireEvent.blur(name);
    await waitFor(() => expect(checked).toContainEqual(['+15551234567', 'Abandoned recipient']), { timeout: 5000 });
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(name.value).toBe('');

    fireEvent.change(number, { target: { value: '+15551234568' } });
    await waitFor(() => expect(checked.some(([to]) => to === '+15551234568')).toBe(true), { timeout: 5000 });
    expect(checked.filter(([to]) => to === '+15551234568').map(([, given]) => given)).toEqual([null]);
    await send('+15551234568', document());
    await waitFor(() => expect(names).toHaveLength(1));
    expect(names[0]).toBeNull();
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

  it('on an unconfirmed answer keeps the number and document, links Sent and names no status code', async () => {
    const keys = faxServer(['accepted']);
    let unconfirmedKey: string | null = null;
    server.use(http.post('/fax', ({ request }) => {
      unconfirmedKey = request.headers.get('Idempotency-Key');
      return HttpResponse.json({ detail: 'Access service is temporarily unavailable.' }, { status: 503 });
    }, { once: true }));
    openFor(us);
    const picked = document();
    await send('2015550123', picked);
    const message = await screen.findByText(/^Faxbot didn't confirm it accepted the fax; it may already be in Faxes → Sent\. To retry without creating a duplicate, send the same document to the same number again, even after reloading this page\.$/);
    expect(message.textContent).not.toMatch(/HTTP|503|Jobs/);
    expect(screen.getByTestId('send-open-sent').getAttribute('href')).toBe('#/faxes/sent');
    // The number and the document stay, so retrying is one click, with the same send identity.
    expect((screen.getByRole('textbox', { name: /Destination Number/ }) as HTMLInputElement).value).toBe('2015550123');
    expect(screen.getByText('referral.pdf')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Send Fax' }));
    expect(await screen.findByText(/^Fax queued/)).toBeTruthy();
    expect(keys).toHaveLength(1);
    expect(unconfirmedKey).toBeTruthy();
    expect(keys[0]).toBe(unconfirmedKey);  // the retry is the same send, never a second fax
    expect(window.sessionStorage.getItem('faxbot_pending_send')).toBeNull();
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
  it('words a route by its own unit, then asks the shared predictor what this document would cost', async () => {
    const asked: string[] = [];
    const predicted: string[] = [];
    let card = { route: 'sip', label: 'Telnyx', rate: '$0.005 a minute, at least 1 minute', one: '0.005', two: '0.01',
      basis: 'Billed as 1 minute at $0.005 a minute; about 46 seconds on the line, for typical pages at a typical fax speed.' };
    server.use(http.get('/routing/destinations/:number', ({ params, request }) => {
      const pages = Number(new URL(request.url).searchParams.get('pages') ?? '1');
      asked.push(`${params.number} ${pages}`);
      return HttpResponse.json({ number: params.number, display_name: null, notes: null, preferred_route: null,
        accepts_references: false, version: 0, routes: [], estimated_cost_30_days: [], direct_partner: null, available_routes: [],
        recommended_routes: [{ route: card.route, label: card.label, reason: 'cheapest',
          explanation: 'Faxbot picks the cheapest route that works reliably.',
          estimated_cost_one_page: { currency: 'USD', amount: card.one }, pages,
          // The older estimate (30 s + 30 s a page) is never shown once the predictor answers.
          estimated_cost: { currency: 'USD', amount: '9.99' }, rate: card.rate,
          included_in_plan: false, monthly_fee: null }] });
    }), http.get('/routing/predict', ({ request }) => {
      const url = new URL(request.url);
      const pages = Number(url.searchParams.get('pages'));
      predicted.push(`${url.searchParams.get('to')} ${pages}`);
      return HttpResponse.json({ to: '+12025550123', number_class: 'local', number_class_text: 'a local number', pages,
        layout: 'normal', resolution: 'fine', sentence: '', note: '',
        routes: [{ route: 'other', label: 'Other', billed_pages: 0, seconds: 40, billed_seconds: 60, seconds_to_next_step: 20,
          cost: { currency: 'USD', amount: '0.50' }, cost_text: '$0.50', marginal: false,
          headline: 'About $0.50 for this 2-page fax.', basis: 'Not this route.' },
        { route: card.route, label: card.label, billed_pages: card.route === 'phaxio' ? 2 : 0, seconds: 46,
          billed_seconds: 60, seconds_to_next_step: 14, cost: { currency: 'USD', amount: card.two }, cost_text: null,
          marginal: false, headline: `About $${card.two} for this 2-page fax.`, basis: card.basis }] });
    }));
    const view = openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    const route = await screen.findByTestId('send-route', {}, { timeout: 2000 });
    expect(route.textContent).toContain('Faxbot will send it through Telnyx.');
    expect(route.textContent).toContain('Faxbot picks the cheapest route that works reliably.');
    // Without a document, the price in the carrier's own unit: never "a page" for a per-minute carrier.
    expect(screen.getByTestId('send-cost').textContent).toBe('About $0.005 a minute, at least 1 minute.');
    expect(screen.queryByTestId('send-cost-basis')).toBeNull();
    const twoPages = new File(['%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Type /Page >> endobj\n'
      + '3 0 obj << /Type /Pages /Count 2 >> endobj\n'], 'two.pdf', { type: 'application/pdf' });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages] } });
    await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
      .toBe('What would this cost? About $0.01 for this 2-page fax.'), { timeout: 2000 });
    expect(screen.getByTestId('send-cost-basis').textContent).toBe(card.basis);
    expect(screen.queryByText(/9\.99/)).toBeNull();
    expect(asked).toEqual(['+12025550123 1', '+12025550123 2']);
    expect(predicted).toContain('+12025550123 2');
    view.unmount();
    card = { route: 'phaxio', label: 'Phaxio', rate: '$0.07 a page', one: '0.07', two: '0.14',
      basis: 'Billed as 2 pages at $0.07 a page; about 46 seconds on the line, for typical pages at a typical fax speed.' };
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages] } });
    await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
      .toBe('What would this cost? About $0.14 for this 2-page fax.'), { timeout: 2000 });
    expect(screen.getByRole('heading', { name: 'Send a fax' })).toBeTruthy();
  });

  it("keeps the route's own price when the predictor can't answer", async () => {
    server.use(http.get('/routing/destinations/:number', ({ params }) => HttpResponse.json({ number: params.number,
      display_name: null, notes: null, preferred_route: null, accepts_references: false, version: 0, routes: [],
      estimated_cost_30_days: [], direct_partner: null, available_routes: [],
      recommended_routes: [{ route: 'sip', label: 'Telnyx', reason: 'cheapest', explanation: 'Cheapest.',
        estimated_cost_one_page: { currency: 'USD', amount: '0.005' }, pages: 2,
        estimated_cost: { currency: 'USD', amount: '0.01' }, rate: '$0.005 a minute, at least 1 minute',
        included_in_plan: false, monthly_fee: null }] })),
    http.get('/routing/predict', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    const twoPages = new File(['%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Type /Page >> endobj\n'
      + '3 0 obj << /Type /Pages /Count 2 >> endobj\n'], 'two.pdf', { type: 'application/pdf' });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages] } });
    await screen.findByTestId('send-route', {}, { timeout: 2000 });
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(screen.getByTestId('send-cost').textContent).toBe('About $0.005 a minute, at least 1 minute.');
    expect(screen.queryByTestId('send-cost-basis')).toBeNull();
    expect(screen.queryByText(/Forbidden/)).toBeNull();
  });

  it('prices the document itself once the number and file settle, without naming its coding', async () => {
    const posted: string[] = [];
    server.use(destinationAnswer(), pageCountPrediction(), http.post('/routing/predict', async ({ request }) => {
      const form = await request.formData();
      posted.push(`${form.get('to')} ${(form.get('file') as File).name} ${[...form.keys()].sort().join(',')}`);
      return HttpResponse.json(documentAnswer(String(form.get('to')), '0.02'));
    }));
    openSend();
    const box = screen.getByRole('textbox', { name: /Destination Number/ });
    // Typed digit by digit, then the file: one request for the settled number and file.
    for (const typed of ['+1202', '+120255', '+12025550123']) fireEvent.change(box, { target: { value: typed } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
    await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
      .toBe('What would this cost? About $0.02 for this measured 2-page fax.'), { timeout: 3000 });
    // The coding comparison belongs to Sent's details ("Fax coding"), not to Send.
    expect(screen.queryByTestId('send-cost-coding')).toBeNull();
    expect(screen.queryByText(/shorter than|MMR/)).toBeNull();
    expect(screen.getByTestId('send-cost-basis').textContent).toBe('Measured on these pages.');
    expect(screen.getByTestId('send-cost-finish').textContent).toBe(
      'From 4 earlier faxes to this number, 9 in 10 calls like this one finish within about 50 seconds.');
    await new Promise((resolve) => setTimeout(resolve, 700));
    // Only the number and the file: never a patient's details.
    expect(posted).toEqual(['+12025550123 two.pdf file,to']);
  });

  it('names the account the measured pages chose, never the page-count route with its price', async () => {
    // By page count Telnyx is recommended; measured, the page account sends one long page for less.
    const measuredWinner = {
      ...documentAnswer('+12025550123', '0.005'),
      sentence: 'Would go through Page trunk as 1 long page: about $0.004 instead of $0.005 through Telnyx as 2 pages.',
      routes: [
        { ...routeAnswer('0.004', 'About $0.004 for this 1-page fax.', 'Billed as 1 page at $0.004 a page.'),
          route: 'sip-pages', label: 'Page trunk', layout: 'dense', sent_pages: 1, original_pages: 2, coding: null },
        { ...routeAnswer('0.005', 'About $0.005 for this 2-page fax.', 'Billed as 1 minute.'),
          layout: 'normal', sent_pages: 2, original_pages: 2, coding: null }],
      selected: { route: 'sip-pages', label: 'Page trunk', layout: 'dense', rendering: 'as_is', coding: 'MMR',
        original_pages: 2, sent_pages: 1, cost: { currency: 'USD', amount: '0.004' }, in_plan: false,
        measured: true },
      runner_up: { route: 'sip', label: 'Telnyx', layout: 'normal', rendering: 'as_is', coding: 'MMR',
        original_pages: 2, sent_pages: 2, cost: { currency: 'USD', amount: '0.005' }, in_plan: false,
        measured: true },
      compared: 2, held: false, approximate: false };
    server.use(destinationAnswer(), pageCountPrediction(),
      http.post('/routing/predict', () => HttpResponse.json(measuredWinner)));
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
    await waitFor(() => expect(screen.getByTestId('send-plan').textContent).toBe(measuredWinner.sentence),
      { timeout: 3000 });
    expect(screen.getByTestId('send-route').textContent).toContain('Faxbot will send it through Page trunk.');
    expect(screen.getByTestId('send-route').textContent).not.toContain('through Telnyx.');
    expect(screen.getByTestId('send-cost').textContent).toBe('What would this cost? About $0.004 for this 1-page fax.');
    expect(screen.getByTestId('send-cost-basis').textContent).toBe('Billed as 1 page at $0.004 a page.');
  });

  it('drops an answer for a number that changed while it was on its way', async () => {
    const answers: Array<(value: Response) => void> = [];
    server.use(destinationAnswer(), pageCountPrediction(), http.post('/routing/predict', async ({ request }) => {
      const to = String((await request.formData()).get('to'));
      if (to === '+12025550123') {
        // The first number's answer comes late, after the number changed.
        await new Promise<void>((resolve) => { answers.push(() => resolve()); });
      }
      return HttpResponse.json(documentAnswer(to, to === '+12025550123' ? '9.99' : '0.03'));
    }));
    openSend();
    const box = screen.getByRole('textbox', { name: /Destination Number/ });
    fireEvent.change(box, { target: { value: '+12025550123' } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
    await waitFor(() => expect(answers).toHaveLength(1), { timeout: 3000 });
    fireEvent.change(box, { target: { value: '+12025550199' } });
    await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
      .toBe('What would this cost? About $0.03 for this measured 2-page fax.'), { timeout: 3000 });
    answers[0](new Response());
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.getByTestId('send-cost').textContent).toBe('What would this cost? About $0.03 for this measured 2-page fax.');
  });

  for (const [status, why] of [[403, 'refused'], [400, 'cannot read the document']] as const) {
    it(`keeps the page-count price when pricing the document is ${why}`, async () => {
      let posts = 0;
      server.use(destinationAnswer(), pageCountPrediction(), http.post('/routing/predict', () => {
        posts += 1;
        return HttpResponse.json({ detail: 'Synthetic refusal' }, { status });
      }));
      openSend();
      fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
      fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
      await waitFor(() => expect(posts).toBe(1), { timeout: 3000 });
      await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
        .toBe('What would this cost? About $0.01 for this 2-page fax.'), { timeout: 3000 });
      expect(screen.queryByTestId('send-cost-coding')).toBeNull();
      expect(screen.queryByText(/Synthetic refusal/)).toBeNull();
    });
  }

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

  it('holds the rows of options a lookup decides, and puts the recipient check below Send', async () => {
    // Structural, as jsdom does no layout: each option's row is reserved above Send while its lookup runs and the
    // option takes that row; information that arrives late renders below Send.
    let release: () => void = () => undefined;
    const late = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get('/routing/destinations/:number', async ({ params }) => {
      await late;
      return HttpResponse.json({ number: params.number, display_name: null, notes: null, preferred_route: null,
        accepts_references: false, version: 0, routes: [], estimated_cost_30_days: [], direct_partner: null,
        // The carrier trunk is set up, so the fax engine is offered once the fax will place a call.
        available_routes: [{ route: 'sip', label: 'Telnyx' }],
        recommended_routes: [{ route: 'local', label: 'This Faxbot', reason: 'own_number',
          explanation: 'One of your own fax numbers.', estimated_cost_one_page: null, rate: null,
          included_in_plan: false, monthly_fee: null }] });
    }), http.get('/batching/check', async () => {
      await late;
      return HttpResponse.json({ number: '+12025550123', sends_together: true, wait_minutes: 10,
        sentence: 'Faxes to this number wait up to 10 minutes to go in one call with others.' });
    }), http.get('/routing/recipient-check', async () => {
      await late;
      return HttpResponse.json({ number: '+12025550123', first_send: true, checked: true, state: 'listed_for_other',
        warning: true, sentence: 'This number is listed for SYNTHETIC IMAGING LLC in NPPES.', name: null, listed: [],
        source_url: 'https://npiregistry.cms.hhs.gov/api-page' });
    }));
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    const sendButton = screen.getByRole('button', { name: 'Send Fax' });
    const above = (element: Element) => Boolean(element.compareDocumentPosition(sendButton) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(above(await screen.findByTestId('send-by-call-pending'))).toBe(true);
    expect(above(screen.getByTestId('send-now-pending'))).toBe(true);
    expect(above(screen.getByTestId('send-engine-pending'))).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 700));  // every lookup has been asked
    release();
    expect(above(await screen.findByTestId('send-by-call', {}, { timeout: 2000 }))).toBe(true);
    expect(above(await screen.findByTestId('send-now', {}, { timeout: 2000 }))).toBe(true);
    expect(screen.queryByTestId('send-by-call-pending')).toBeNull();
    expect(screen.queryByTestId('send-now-pending')).toBeNull();
    expect(screen.queryByTestId('send-engine-pending')).toBeNull();
    // One of your own numbers goes straight into Received with no call, so no engine, until a real call is asked for.
    expect(screen.queryByTestId('send-engine')).toBeNull();
    fireEvent.click(screen.getByTestId('send-by-call').querySelector('input') as HTMLInputElement);
    expect(above(await screen.findByTestId('send-engine'))).toBe(true);
    expect(above(await screen.findByTestId('send-recipient-check', {}, { timeout: 2000 }))).toBe(false);
  });

  it('says the plan once when the route was chosen because faxes are included in it', async () => {
    const plan = 'Included in your HumbleFax plan ($10 a month)';
    const basis = `${plan}: this fax adds nothing to the bill; about 19 seconds on the line, for typical pages at a `
      + "typical fax speed. You've used 5 of the 200 pages you set as this plan's normal-use budget this month.";
    server.use(http.get('/routing/destinations/:number', ({ params }) => HttpResponse.json({ number: params.number,
      display_name: null, notes: null, preferred_route: null, accepts_references: false, version: 0, routes: [],
      estimated_cost_30_days: [], direct_partner: null, available_routes: [],
      recommended_routes: [{ route: 'humblefax', label: 'HumbleFax', reason: 'included', explanation: `${plan}.`,
        estimated_cost_one_page: null, pages: 2, estimated_cost: null, rate: null, included_in_plan: true,
        monthly_fee: { currency: 'USD', amount: '10' } }] })),
    http.get('/routing/predict', () => HttpResponse.json({ to: '+12025550123', number_class: 'local',
      number_class_text: 'a local number', pages: 2, layout: 'normal', resolution: 'fine', sentence: '', note: '',
      routes: [{ route: 'humblefax', label: 'HumbleFax', billed_pages: 0, seconds: 19, billed_seconds: null,
        seconds_to_next_step: null, cost: { currency: 'USD', amount: '0' }, cost_text: null, marginal: true,
        headline: 'Nothing extra for this fax.', basis, finish_sentence: null, time_note: null }] })));
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    // Before the estimate, the route's reason names the plan.
    expect((await screen.findByTestId('send-route-reason', {}, { timeout: 2000 })).textContent).toBe(`${plan}.`);
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
    await waitFor(() => expect(screen.getByTestId('send-cost-basis').textContent).toBe(basis), { timeout: 2000 });
    // The estimate opens with the plan and its cost, so each is said once, not twice or three times (D23).
    expect(screen.getByTestId('send-route').textContent!.split(plan)).toHaveLength(2);
    expect(screen.getByTestId('send-route').textContent!.match(/nothing/g)).toHaveLength(1);
    expect(screen.queryByTestId('send-route-reason')).toBeNull();
    expect(screen.queryByTestId('send-cost')).toBeNull();
  });

  it('never moves Send or Urgent when the estimate arrives late', async () => {
    // jsdom does no layout, so this checks the structure that guarantees it: the route and estimate render below
    // Send and Clear, and nothing above Send changes when the late estimate lands.
    let release: () => void = () => undefined;
    const late = new Promise<void>((resolve) => { release = resolve; });
    server.use(destinationAnswer(), http.get('/routing/predict', async () => {
      await late;
      return HttpResponse.json({ to: '+12025550123', number_class: 'local', number_class_text: 'a local number',
        pages: 2, layout: 'normal', resolution: 'fine', sentence: '', note: '',
        routes: [{ ...routeAnswer('0.01', 'About $0.01 for this 2-page fax.', 'Billed as 1 minute at $0.005 a '
          + 'minute; about 46 seconds on the line, for typical pages at a typical fax speed.'),
          finish_sentence: null,
          time_note: 'The time on the line is an estimate until Faxbot has timed 3 faxes to this number.' }] });
    }));
    faxServer(['accepted']);
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
    // The route line is in; its price is still on its way.
    await screen.findByTestId('send-route', {}, { timeout: 2000 });
    expect(screen.queryByTestId('send-cost-basis')).toBeNull();
    const sendButton = screen.getByRole('button', { name: 'Send Fax' });
    const urgent = screen.getByTestId('send-urgent').querySelector('input') as HTMLInputElement;
    const textAbove = () => {
      const range = window.document.createRange();
      range.setStart(window.document.body, 0);
      range.setEndBefore(sendButton);
      return range.toString();
    };
    const before = textAbove();
    const below = (element: Element) => Boolean(sendButton.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(below(screen.getByTestId('send-route'))).toBe(true);
    release();
    await waitFor(() => expect(screen.getByTestId('send-cost-time-note')).toBeTruthy(), { timeout: 2000 });
    // The estimate landed below Send; everything above Send, Urgent included, is exactly as it was.
    expect(textAbove()).toBe(before);
    expect(below(screen.getByTestId('send-cost-basis'))).toBe(true);
    expect(urgent.compareDocumentPosition(sendButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(sendButton);
    expect(await screen.findByText(/^Fax queued/)).toBeTruthy();
    expect(urgent.checked).toBe(false);
  });
});

// D23 (journey pass 2026-10-15, acceptance deploy-3e8997bc): a 1-page fax to a number HumbleFax would carry while
// HumbleFax is past its normal-use budget. The panel said "nothing extra" three times beside "this fax would go past
// that"; it now says the cost once and the budget once, with what Faxbot does about it.
describe('The estimate tells one story', () => {
  const plan = 'Included in your HumbleFax plan ($10 a month): this fax adds nothing to the bill';
  const overBasis = `${plan}; about 23 seconds on the line, for typical pages at a typical fax speed. You've used 245 `
    + "of the 200 pages you set as this plan's normal-use budget this month, so Faxbot sends by your other routes first "
    + 'when one of them can take the fax.';
  // HumbleFax first, past its budget because no other route can take the fax; the trunk later in the order.
  const overBudget = () => http.get('/routing/destinations/:number', ({ params }) => HttpResponse.json({
    number: params.number, display_name: null, notes: null, preferred_route: null, accepts_references: false,
    version: 0, routes: [], estimated_cost_30_days: [], direct_partner: null,
    available_routes: [{ route: 'humblefax', label: 'HumbleFax' }, { route: 'sip', label: 'Telnyx' }],
    recommended_routes: [
      { route: 'humblefax', label: 'HumbleFax', reason: 'over_budget',
        explanation: 'Included in your HumbleFax plan, though it is past its normal-use budget this month; Faxbot has '
          + 'no other reliable route for this fax.',
        estimated_cost_one_page: null, pages: 1, estimated_cost: null, rate: null, included_in_plan: true,
        monthly_fee: { currency: 'USD', amount: '10' } },
      { route: 'sip', label: 'Telnyx', reason: 'unreliable', explanation: 'Recent faxes to this number often failed.',
        estimated_cost_one_page: { currency: 'USD', amount: '0.005' }, rate: '$0.005 a minute, at least 1 minute',
        included_in_plan: false, monthly_fee: null }] }));
  const planRoute = { route: 'humblefax', label: 'HumbleFax', billed_pages: 0, seconds: 23, billed_seconds: null,
    seconds_to_next_step: null, cost: { currency: 'USD', amount: '0' }, cost_text: null, marginal: true,
    headline: 'Nothing extra for this fax.', basis: overBasis, finish_sentence: null, time_note: null };
  const pageCount = () => http.get('/routing/predict', () => HttpResponse.json({ to: '+19725550123',
    number_class: 'local', number_class_text: 'a local number', pages: 1, layout: 'normal', resolution: 'fine',
    sentence: '', note: '', routes: [planRoute] }));
  // POST /routing/predict as the server words a plan's measured choice (selections.sentence, phase preview).
  const measured = (runnerUp: object | null, sentence: string) => http.post('/routing/predict', () => HttpResponse.json({
    to: '+19725550123', number_class: 'local', number_class_text: 'a local number', pages: 1, layout: 'normal',
    resolution: 'fine', sentence, note: '', measured: { MMR: 82000 }, measured_sentence: null, jbig_measured: false,
    routes: [{ ...planRoute, cost: null, coding: null, layout: 'normal', sent_pages: 1, original_pages: 1 }],
    selected: { route: 'humblefax', label: 'HumbleFax', layout: 'normal', rendering: 'as_is', coding: null,
      original_pages: 1, sent_pages: 1, cost: null, in_plan: true, measured: true },
    runner_up: runnerUp, compared: runnerUp ? 2 : 1, held: false, approximate: false }));
  const fill = () => {
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+19725550123' } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
  };

  it('says the cost once and the budget once for the measured choice, as acceptance showed it', async () => {
    server.use(overBudget(), pageCount(), measured(null, 'Would go through HumbleFax as 1 page: nothing extra.'));
    openSend();
    fill();
    await waitFor(() => expect(screen.getByTestId('send-cost-basis').textContent).toBe(overBasis), { timeout: 3000 });
    const panel = screen.getByTestId('send-route').textContent!;
    expect(panel).toContain('Faxbot will send it through HumbleFax.');
    expect(panel.match(/nothing/gi)).toHaveLength(1);
    expect(panel).not.toContain('would go past');
    expect(screen.queryByTestId('send-plan')).toBeNull();
    expect(screen.queryByTestId('send-cost')).toBeNull();
    // D27 on the same screen: the trunk is only later in the order, so the engine is worded as that case.
    expect(screen.getByTestId('send-engine').textContent).toContain('Fax engine, if it goes over your phone line');
  });

  it('keeps the measured choice when it compares the plan with a priced route', async () => {
    const sentence = 'Would go through HumbleFax as 1 page: nothing extra instead of $0.005 through Telnyx as 1 page.';
    server.use(overBudget(), pageCount(), measured({ route: 'sip', label: 'Telnyx', layout: 'normal',
      rendering: 'as_is', coding: 'MR', original_pages: 1, sent_pages: 1, cost: { currency: 'USD', amount: '0.005' },
      in_plan: false, measured: true }, sentence));
    openSend();
    fill();
    await waitFor(() => expect(screen.getByTestId('send-plan').textContent).toBe(sentence), { timeout: 3000 });
    expect(screen.getByTestId('send-cost-basis').textContent).toBe(overBasis);
    expect(screen.queryByTestId('send-cost')).toBeNull();
  });

  it('says the cost once and the budget once from the page count when the document is not measured', async () => {
    server.use(overBudget(), pageCount(), http.post('/routing/predict', () =>
      HttpResponse.json({ detail: 'Synthetic refusal' }, { status: 400 })));
    openSend();
    fill();
    await waitFor(() => expect(screen.getByTestId('send-cost-basis').textContent).toBe(overBasis), { timeout: 3000 });
    const panel = screen.getByTestId('send-route').textContent!;
    expect(panel.match(/nothing/gi)).toHaveLength(1);
    expect(panel.split('Included in your HumbleFax plan')).toHaveLength(2);
    expect(screen.queryByTestId('send-route-reason')).toBeNull();
    expect(screen.queryByTestId('send-cost')).toBeNull();
  });
});

describe('The fax engine for one fax', () => {
  // Each number's routes by key: 'sip' is the carrier trunk, so Faxbot's own fax engines place its calls.
  const routesFor = (byNumber: Record<string, string[]>) => http.get('/routing/destinations/:number', ({ params }) => {
    const keys = byNumber[String(params.number)] ?? [];
    return HttpResponse.json({ number: params.number, display_name: null, notes: null, preferred_route: null,
      accepts_references: false, version: 0, routes: [], estimated_cost_30_days: [], direct_partner: null,
      available_routes: keys.map((route) => ({ route, label: route })),
      recommended_routes: keys.map((route) => ({ route, label: route === 'phaxio' ? 'Phaxio' : 'Telnyx',
        reason: 'cheapest', explanation: 'Cheapest.', estimated_cost_one_page: null, rate: null,
        included_in_plan: false, monthly_fee: null })) });
  });
  // The engine field of each fax posted (null when Send left it out).
  const engineServer = () => {
    const posted: Array<string | null> = [];
    server.use(http.post('/fax', async ({ request }) => {
      posted.push((await request.formData()).get('engine') as string | null);
      return HttpResponse.json({ id: 'b'.repeat(32), status: 'queued', delivery_state: 'ready' }, { status: 202 });
    }));
    return posted;
  };
  const numberBox = () => screen.getByRole('textbox', { name: /Destination Number/ });
  const chooseEngine = async (name: RegExp) => {
    fireEvent.mouseDown(within(await screen.findByTestId('send-engine', {}, { timeout: 2000 })).getByRole('combobox'));
    fireEvent.click(await screen.findByRole('option', { name }));
  };
  const attachAndSend = (file: File) => {
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: 'Send Fax' }));
  };

  it('offers it for a number your carrier trunk can call, Automatic unless you pick one for this fax', async () => {
    server.use(routesFor({ '+12025550123': ['sip'] }));
    const posted = engineServer();
    openSend();
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    const control = await screen.findByTestId('send-engine', {}, { timeout: 2000 });
    expect(control.textContent).toContain('Fax engine');
    expect(control.textContent).toContain('Automatic');
    expect(control.textContent).toContain(ENGINE_CHOICES.find((choice) => choice.value === 'auto')!.hint);
    attachAndSend(document());
    expect(await screen.findByText(/^Fax queued/)).toBeTruthy();
    // Automatic: Faxbot chooses, so Send posts no engine.
    expect(posted).toEqual([null]);

    // The next fax, on the built-in engine; each choice is named in plain words with its hint, never a raw value.
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    fireEvent.mouseDown(within(await screen.findByTestId('send-engine', {}, { timeout: 2000 })).getByRole('combobox'));
    const options = await screen.findAllByRole('option');
    expect(options.map((option) => option.textContent)).toEqual(ENGINE_CHOICES.map((choice) => choice.name + choice.hint));
    expect(options.map((option) => option.textContent).join(' ')).not.toMatch(/\bauto\b|builtin|hylafax/);
    fireEvent.click(screen.getByRole('option', { name: /^Built-in engine/ }));
    await waitFor(() => expect(screen.getByTestId('send-engine').textContent)
      .toContain(ENGINE_CHOICES.find((choice) => choice.value === 'builtin')!.hint));
    attachAndSend(document(1_790_000_000_002));
    await waitFor(() => expect(posted).toEqual([null, 'builtin']));
    // The choice was for that one fax: the next starts on Automatic again.
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    await waitFor(() => expect(screen.getByTestId('send-engine').textContent).toContain('Automatic'), { timeout: 2000 });
  });

  // The server lists every route the installation has as available_routes, so in practice the control follows whether
  // a trunk is set up; this fixture varies them per number to prove a pick is posted only while the control is shown.
  it('posts a pick only while it is offered, never for a number without a trunk among its routes', async () => {
    server.use(routesFor({ '+12025550123': ['sip'], '+12025550199': ['phaxio'] }));
    const posted = engineServer();
    openSend();
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    await chooseEngine(/^Built-in engine/);
    fireEvent.change(numberBox(), { target: { value: '+12025550199' } });
    await waitFor(() => expect(screen.getByTestId('send-route').textContent).toContain('through Phaxio.'),
      { timeout: 2000 });
    expect(screen.queryByTestId('send-engine')).toBeNull();
    expect(screen.queryByTestId('send-engine-pending')).toBeNull();
    attachAndSend(document());
    await waitFor(() => expect(posted).toEqual([null]));
  });

  it('retries after a reload with the engine picked before, so the server sees the same fax', async () => {
    server.use(routesFor({ '+12025550123': ['sip'] }));
    const answers: Answer[] = ['lost', 'accepted'];
    const posted: string[] = [];
    server.use(http.post('/fax', async ({ request }) => {
      posted.push(`${request.headers.get('Idempotency-Key')} ${(await request.formData()).get('engine')}`);
      if (answers.shift() === 'lost') return HttpResponse.error();
      return HttpResponse.json({ id: 'b'.repeat(32), status: 'queued', delivery_state: 'ready' }, { status: 202 });
    }));
    const first = openSend();
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    await chooseEngine(/^Built-in engine/);
    attachAndSend(document());
    expect(await screen.findByText(lostAnswer)).toBeTruthy();
    first.unmount(); // the page is reloaded, and with it the pick

    openSend();
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    await screen.findByTestId('send-engine', {}, { timeout: 2000 });
    attachAndSend(document());
    expect(await screen.findByText(/^Fax queued/)).toBeTruthy();
    expect(posted).toHaveLength(2);
    expect(posted[1]).toBe(posted[0]);
    expect(posted[0]).toMatch(/ builtin$/);
    // Accepted: nothing is kept, and the next fax starts on Automatic.
    expect(window.sessionStorage.getItem('faxbot_pending_send')).toBeNull();
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    await waitFor(() => expect(screen.getByTestId('send-engine').textContent).toContain('Automatic'), { timeout: 2000 });
  });

  it('leaves it out for someone who may not see routes', async () => {
    server.use(http.get('/routing/destinations/:number', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    openSend();
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    await waitFor(() => expect(screen.queryByTestId('send-engine-pending')).toBeNull(), { timeout: 2000 });
    expect(screen.queryByTestId('send-engine')).toBeNull();
  });

  it('offers it for each of your trunk accounts, not only the first', async () => {
    server.use(routesFor({ '+12025550123': ['branch'], '+12025550199': ['phaxio'] }),
      http.get('/admin/providers/accounts', () => HttpResponse.json({ generation: 1, default_sending: 'sip',
        default_receiving: null, providers: [], sites: [],
        accounts: [{ key: 'sip', provider: 'sip' }, { key: 'branch', provider: 'sip' }, { key: 'phaxio', provider: 'phaxio' }] })));
    openSend();
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    expect(await screen.findByTestId('send-engine', {}, { timeout: 2000 })).toBeTruthy();
    fireEvent.change(numberBox(), { target: { value: '+12025550199' } });
    await waitFor(() => expect(screen.getByTestId('send-route').textContent).toContain('through Phaxio.'),
      { timeout: 2000 });
    expect(screen.queryByTestId('send-engine')).toBeNull();
  });

  it('asks for the estimate again for the engine you pick, and its late answer never moves Send', async () => {
    const asked: Array<string | null> = [];
    let release: () => void = () => undefined;
    const late = new Promise<void>((resolve) => { release = resolve; });
    server.use(routesFor({ '+12025550123': ['sip'] }), pageCountPrediction(),
      http.post('/routing/predict', async ({ request }) => {
        const engine = (await request.formData()).get('engine') as string | null;
        asked.push(engine);
        if (engine === 'builtin') await late;
        return HttpResponse.json(documentAnswer('+12025550123', engine === 'builtin' ? '0.03' : '0.02'));
      }));
    openSend();
    fireEvent.change(numberBox(), { target: { value: '+12025550123' } });
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
    await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
      .toBe('What would this cost? About $0.02 for this measured 2-page fax.'), { timeout: 3000 });
    // Automatic: the estimate is asked for without an engine, as Send posts the fax.
    expect(asked).toEqual([null]);
    await chooseEngine(/^Built-in engine/);
    await waitFor(() => expect(asked).toEqual([null, 'builtin']), { timeout: 3000 });
    const sendButton = screen.getByRole('button', { name: 'Send Fax' });
    const textAbove = () => {
      const range = window.document.createRange();
      range.setStart(window.document.body, 0);
      range.setEndBefore(sendButton);
      return range.toString();
    };
    const before = textAbove();
    release();
    await waitFor(() => expect(screen.getByTestId('send-cost').textContent)
      .toBe('What would this cost? About $0.03 for this measured 2-page fax.'), { timeout: 3000 });
    // The built-in engine's estimate landed below Send; everything above Send is exactly as it was.
    expect(textAbove()).toBe(before);
    expect(Boolean(sendButton.compareDocumentPosition(screen.getByTestId('send-cost'))
      & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
  });
});

// D27 (journey pass 2026-10-15): the engine only matters for a fax over your phone line (a trunk), so the choice follows
// the routes this fax would try, never merely whether a trunk is set up.
describe('The fax engine follows the route the fax takes', () => {
  const destination = (recommended: Array<[string, string]>, available: string[]) =>
    http.get('/routing/destinations/:number', ({ params }) => HttpResponse.json({ number: params.number,
      display_name: null, notes: null, preferred_route: null, accepts_references: false, version: 0, routes: [],
      estimated_cost_30_days: [], direct_partner: null,
      available_routes: available.map((route) => ({ route, label: route })),
      recommended_routes: recommended.map(([route, label], index) => ({ route, label,
        reason: index ? 'alternative' : 'included', explanation: index ? 'Used if the routes above it are unavailable.'
          : `Included in your ${label} plan.`, estimated_cost_one_page: null, rate: null,
        included_in_plan: !index, monthly_fee: null })) }));
  const numberBox = () => screen.getByRole('textbox', { name: /Destination Number/ });
  const fallbackHint = 'Faxbot plans to send it through HumbleFax. This applies only if it goes over your phone line '
    + 'instead.';

  it('leaves it out when the fax goes by a fax service and no trunk is in its order, though a trunk is set up', async () => {
    server.use(destination([['humblefax', 'HumbleFax']], ['humblefax', 'sip']));
    const posted: Array<string | null> = [];
    server.use(http.post('/fax', async ({ request }) => {
      posted.push((await request.formData()).get('engine') as string | null);
      return HttpResponse.json({ id: 'b'.repeat(32), status: 'queued', delivery_state: 'ready' }, { status: 202 });
    }));
    openSend();
    fireEvent.change(numberBox(), { target: { value: '+19725550123' } });
    await waitFor(() => expect(screen.getByTestId('send-route').textContent).toContain('through HumbleFax.'),
      { timeout: 2000 });
    expect(screen.queryByTestId('send-engine-pending')).toBeNull();
    expect(screen.queryByTestId('send-engine')).toBeNull();
    await send('+19725550123', document());
    await waitFor(() => expect(posted).toEqual([null]));
  });

  it('words it as the phone line case when the trunk is only a fallback, and still asks the estimate again', async () => {
    const asked: Array<string | null> = [];
    server.use(destination([['humblefax', 'HumbleFax'], ['sip', 'Telnyx']], ['humblefax', 'sip']),
      http.post('/routing/predict', async ({ request }) => {
        asked.push((await request.formData()).get('engine') as string | null);
        return HttpResponse.json({ detail: 'Synthetic refusal' }, { status: 400 });
      }),
      // The price from the page count: no route priced, so no estimate shows.
      http.get('/routing/predict', () => HttpResponse.json({ to: '+19725550123', number_class: 'local',
        number_class_text: 'a local number', pages: 2, layout: 'normal', resolution: 'fine', sentence: '', note: '',
        routes: [] })));
    openSend();
    fireEvent.change(numberBox(), { target: { value: '+19725550123' } });
    const control = await screen.findByTestId('send-engine', {}, { timeout: 2000 });
    expect(control.textContent).toContain('Fax engine, if it goes over your phone line');
    expect(screen.getByTestId('send-engine-hint').textContent).toBe(fallbackHint);
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
    await waitFor(() => expect(asked).toEqual([null]), { timeout: 3000 });
    fireEvent.mouseDown(within(control).getByRole('combobox'));
    fireEvent.click(await screen.findByRole('option', { name: /^Built-in engine/ }));
    await waitFor(() => expect(asked).toEqual([null, 'builtin']), { timeout: 3000 });
    // The hint stays the phone line case; the pick is shown in the field.
    expect(screen.getByTestId('send-engine-hint').textContent).toBe(fallbackHint);
    expect(screen.getByTestId('send-engine').textContent).toContain('Built-in engine');
  });

  it('words it plainly once the measured choice goes over the trunk', async () => {
    server.use(destination([['humblefax', 'HumbleFax'], ['sip', 'Telnyx']], ['humblefax', 'sip']), pageCountPrediction(),
      http.post('/routing/predict', () => HttpResponse.json({
        ...documentAnswer('+19725550123', '0.005'), sentence: 'Would go through Telnyx as 2 pages: about $0.005.',
        selected: { route: 'sip', label: 'Telnyx', layout: 'normal', rendering: 'as_is', coding: 'MR', original_pages: 2,
          sent_pages: 2, cost: { currency: 'USD', amount: '0.005' }, in_plan: false, measured: true },
        runner_up: null, compared: 2, held: false, approximate: false })));
    openSend();
    fireEvent.change(numberBox(), { target: { value: '+19725550123' } });
    expect((await screen.findByTestId('send-engine', {}, { timeout: 2000 })).textContent)
      .toContain('Fax engine, if it goes over your phone line');
    fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [twoPages()] } });
    await waitFor(() => expect(screen.getByTestId('send-plan').textContent)
      .toBe('Would go through Telnyx as 2 pages: about $0.005.'), { timeout: 3000 });
    expect(screen.getByTestId('send-engine').textContent).not.toContain('if it goes over your phone line');
    expect(screen.getByTestId('send-engine-hint').textContent)
      .toBe(ENGINE_CHOICES.find((choice) => choice.value === 'auto')!.hint);
  });
});

describe("The patient, for a recipient's health record system", () => {
  const routes = (keys: Array<[string, string]>) => http.get('/routing/destinations/:number', ({ params }) =>
    HttpResponse.json({ number: params.number, display_name: null, notes: null, preferred_route: null,
      accepts_references: false, version: 0, routes: [], estimated_cost_30_days: [], direct_partner: null,
      available_routes: [], recommended_routes: keys.map(([route, label]) => ({ route, label, reason: 'cheapest',
        explanation: 'Cheapest.', estimated_cost_one_page: null, rate: null, included_in_plan: false,
        monthly_fee: null })) }));

  it('asks for the patient only when a health record system is among the routes, and sends what was given', async () => {
    server.use(routes([['phaxio', 'Phaxio'], [`fhir:${'e'.repeat(32)}`, 'Synthetic Hospital (FHIR)']]));
    faxServer(['accepted']);
    const appended = vi.spyOn(FormData.prototype, 'append');
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    const section = await screen.findByTestId('send-patient', {}, { timeout: 2000 });
    expect(section.textContent).toContain('Synthetic Hospital (FHIR) files this document in its health records.');
    fireEvent.change(screen.getByTestId('send-patient-number'), { target: { value: 'MRN-5550199' } });
    fireEvent.change(screen.getByTestId('send-patient-family'), { target: { value: 'Zyxwvut' } });
    fireEvent.change(screen.getByTestId('send-patient-birth-date'), { target: { value: '1961-07-23' } });
    await send('+12025550123', document());
    expect(await screen.findByText(/Fax queued/)).toBeTruthy();
    const sent = appended.mock.calls.filter(([name]) => String(name).startsWith('patient_'))
      .map(([name, value]) => `${name}=${value}`);
    expect(sent).toEqual(['patient_record_number=MRN-5550199', 'patient_family_name=Zyxwvut',
      'patient_birth_date=1961-07-23']);
    appended.mockRestore();
    // Never kept in the browser, and cleared with the form after the send.
    expect(JSON.stringify({ ...window.sessionStorage, ...window.localStorage })).not.toContain('MRN-5550199');
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    await screen.findByTestId('send-patient', {}, { timeout: 2000 });
    expect((screen.getByTestId('send-patient-number') as HTMLInputElement).value).toBe('');
  });

  it('lets someone who may not read routes add the patient on request', async () => {
    server.use(http.get('/routing/destinations/:number', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })));
    faxServer(['accepted']);
    const appended = vi.spyOn(FormData.prototype, 'append');
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    const add = await screen.findByTestId('send-patient-add', {}, { timeout: 2000 });
    expect(screen.queryByTestId('send-patient')).toBeNull();
    fireEvent.click(add);
    expect(screen.getByTestId('send-patient').textContent)
      .toContain('Only for a recipient whose health record system takes your documents.');
    fireEvent.change(screen.getByTestId('send-patient-number'), { target: { value: 'MRN-5550199' } });
    await send('+12025550123', document());
    await screen.findByText(/Fax queued/);
    expect(appended.mock.calls.filter(([name]) => String(name).startsWith('patient_'))
      .map(([name, value]) => `${name}=${value}`)).toEqual(['patient_record_number=MRN-5550199']);
    appended.mockRestore();
  });

  it('leaves the patient out for a number without a health record system', async () => {
    server.use(routes([['phaxio', 'Phaxio']]));
    faxServer(['accepted']);
    const appended = vi.spyOn(FormData.prototype, 'append');
    openSend();
    fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+12025550123' } });
    await screen.findByTestId('send-route', {}, { timeout: 2000 });
    expect(screen.queryByTestId('send-patient')).toBeNull();
    await send('+12025550123', document());
    await screen.findByText(/Fax queued/);
    expect(appended.mock.calls.some(([name]) => String(name).startsWith('patient_'))).toBe(false);
    appended.mockRestore();
  });
});
