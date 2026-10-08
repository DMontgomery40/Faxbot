// Encoded pages (experimental): the per-number opt-in in a fax number's Details, the sent fax's one page line,
// and a received fax's decode result with its download.
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList from '../components/JobsList';
import { EncodedPagesPanel, ReceivedEncodedPages } from '../components/delivery/EncodedPages';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const NUMBER = '+12025550123';
const JOB = 'd'.repeat(32);

const off = {
  number: NUMBER, enabled: false, style: 'dense', fec: 'medium', has_key: false, key_fingerprint: null, version: 0,
  state_sentence: 'Off: faxes to this number go as normal pages.', agreement: null, history: [],
  agreement_text: 'The recipient agreed to receive documents as encoded pages that their Faxbot, or the decoder at '
    + 'faxbot.net/decode, turns back into the original.',
  limits_text: 'Experimental. Use it only for recipients outside HIPAA-style rules: an encoded page is not readable '
    + 'as a fax until it is decoded.',
};

describe('Encoded pages in a fax number\'s Details', () => {
  it('turns on only after the recipient\'s agreement is recorded and never shows the shared key again', async () => {
    const saved: unknown[] = [];
    server.use(
      http.get('/codec/numbers/:number', () => HttpResponse.json(off)),
      http.put('/codec/numbers/:number', async ({ request }) => {
        saved.push(await request.json());
        return HttpResponse.json({
          ...off, enabled: true, version: 1, fec: 'high', has_key: true, key_fingerprint: '3f2a9c1d',
          state_sentence: 'On: when encoded pages cost less on the fax’s route, faxes to this number go as '
            + 'encoded pages (experimental).',
          agreement: { action: 'on', by: 'Owner', at: '2026-10-07T16:30:00+00:00', recipient_agreed: true,
            style: 'Dense pages', fec: 'High', key_fingerprint: '3f2a9c1d' },
        });
      }),
    );
    render(<EncodedPagesPanel client={client()} number={NUMBER} canWrite />);
    expect(await screen.findByText(off.state_sentence)).toBeTruthy();
    expect(screen.getByText(off.limits_text)).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Send documents to this number as encoded pages when that costs less' }));
    const save = screen.getByRole('button', { name: 'Save encoded pages' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(screen.getByRole('checkbox', { name: off.agreement_text }));
    fireEvent.change(screen.getByLabelText('Error correction'), { target: { value: 'high' } });
    fireEvent.change(screen.getByLabelText('Shared key (optional)'), { target: { value: 'synthetic partner key' } });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    expect(await screen.findByText('Documents are encrypted with the shared key whose fingerprint is 3f2a9c1d.')).toBeTruthy();
    expect(saved).toEqual([{ enabled: true, recipient_agreed: true, style: 'dense', fec: 'high', version: 0,
      clear_key: false, shared_key: 'synthetic partner key' }]);
    expect((screen.getByLabelText('Shared key (optional)') as HTMLInputElement).value).toBe('');
    expect(screen.getByText(/The recipient's agreement was recorded by Owner on/)).toBeTruthy();
  });

  it('shows a person without settings access the setting without a save button', async () => {
    server.use(http.get('/codec/numbers/:number', () => HttpResponse.json(off)));
    render(<EncodedPagesPanel client={client()} number={NUMBER} canWrite={false} />);
    expect(await screen.findByText(off.state_sentence)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /encoded pages/ })).toBeNull();
  });
});

describe('Encoded pages on a sent and a received fax', () => {
  it('says once, in the page line, that the newest attempt went as encoded pages', async () => {
    const sentence = 'Sent as 1 encoded page instead of 23 (experimental).';
    const job = { id: JOB, to_number: '+15550100001', status: 'success', backend: 'sinch', pages: 23,
      created_at: '2026-10-07T12:00:00', updated_at: '2026-10-07T12:01:00', delivery_state: 'success',
      dispatch_mode: 'normal', delivery_version: 3,
      page_layout: { layout: 'codec', original_pages: 23, sent_pages: 1, sentences: [sentence] } };
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job)),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+15550100001'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    const line = await within(dialog).findByTestId('job-pages');
    expect(line.textContent).toBe(`How the pages were sent${sentence}`);
    expect(within(dialog).getAllByText(sentence)).toHaveLength(1);
    expect(within(dialog).queryByText('Encoded pages')).toBeNull();
  });

  it('shows the decode result, offers the original, and gives the fallback sentence when decoding failed', async () => {
    server.use(http.get('/codec/received/:fax', ({ params }) => HttpResponse.json(params.fax === 'good'
      ? { encoded: true, state: 'decoded', content_type: 'application/pdf', document_name: 'referral.pdf',
        sentence: 'Carried an encoded document on 1 page; Faxbot decoded it and checked its fingerprint (experimental).' }
      : { encoded: true, state: 'failed', sentence: 'The document is encrypted with a key this Faxbot does not have, '
        + 'so the fax is delivered as received.' })));
    const { unmount } = render(<ReceivedEncodedPages client={client()} faxId="good" canDownload />);
    expect(await screen.findByText(/Faxbot decoded it and checked its fingerprint/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Download the original document' })).toBeTruthy();
    unmount();
    render(<ReceivedEncodedPages client={client()} faxId="bad" canDownload />);
    expect(await screen.findByText(/so the fax is delivered as received\./)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download the original document' })).toBeNull();
  });
});
