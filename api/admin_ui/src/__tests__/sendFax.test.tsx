// A send keeps its Idempotency-Key across a reload, so retrying after a lost
// answer cannot send the fax twice.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
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
  fireEvent.change(screen.getByPlaceholderText('+15551234567'), { target: { value: number } });
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
