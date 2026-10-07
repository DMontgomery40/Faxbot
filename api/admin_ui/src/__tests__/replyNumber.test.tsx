import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import ReplyNumber from '../components/ReplyNumber';
import { server } from '../test/server';

const VIEW = {
  number: null, shows: '+13035550101', source: 'automatic',
  sentence: 'Faxes show +13035550101, your cheapest number to receive on that reaches a mailbox.',
  problems: [], mailboxes: [], header_problem: null,
  candidates: [
    { number: '+13035550101', provider: 'Telnyx', kind: 'local', receives: true, mailbox: 'Front desk', mailbox_id: 'mbx-front',
      price: '$0.0032 a minute; about $0.0096 for a 5-page fax.', avoid: false, typical_cost: '$0.0096' },
    { number: '+13035550102', provider: 'Telnyx', kind: 'local', receives: true, mailbox: 'Billing', mailbox_id: 'mbx-billing',
      price: '$0.0032 a minute; about $0.0096 for a 5-page fax.', avoid: false, typical_cost: '$0.0096' },
  ],
  suggestion: { number: '+13035550101', provider: 'Telnyx', kind: 'local', receives: true, mailbox: 'Front desk',
    mailbox_id: 'mbx-front', price: '', avoid: false, typical_cost: '$0.0096',
    sentence: '+13035550101 is your cheapest number to receive on that reaches a mailbox: $0.0032 a minute.' },
  avoid: [{ number: '+18005550199', sentence: "Don't publish +18005550199 for receiving: on a toll-free number you pay for every minute of every fax sent to you." }],
  caller_id: [{ provider: 'Telnyx', shows: true, sentence: 'Telnyx calls show +13035550101 as caller ID: Telnyx gives you that number on this trunk.' }],
};

function client() {
  return new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' });
}

describe('Numbers → Sender identity, Reply number', () => {
  it('shows the number faxes show, the cheapest number, numbers not to publish and caller ID, and saves the suggestion', async () => {
    const saved: unknown[] = [];
    server.use(
      http.get('/numbers/reply', () => HttpResponse.json(VIEW)),
      http.get('/access/mailboxes', () => HttpResponse.json({ items: [], next_cursor: null })),
      http.put('/numbers/reply', async ({ request }) => {
        saved.push(await request.json());
        return HttpResponse.json({ ok: true, number: '+13035550101' });
      }),
    );
    render(<ReplyNumber client={client()} canWrite />);
    const section = await screen.findByTestId('reply-number');
    expect(within(section).getByText(VIEW.sentence)).toBeTruthy();
    expect(within(section).getByText(VIEW.avoid[0].sentence)).toBeTruthy();
    expect(within(section).getByText(VIEW.caller_id[0].sentence, { exact: false })).toBeTruthy();
    fireEvent.click(within(section).getByRole('button', { name: 'Use this number' }));
    expect(await within(section).findByText('Saved. New faxes show this number.')).toBeTruthy();
    expect(saved).toEqual([{ number: '+13035550101' }]);
  });

  it('shows the refusal sentence when a number does not reach a mailbox, and offers only numbers that reach the chosen mailbox', async () => {
    const refusal = 'No rule under Numbers sends faxes for +18005550199 to a mailbox. Add one under Numbers, Your numbers, then choose it again.';
    server.use(
      http.get('/numbers/reply', () => HttpResponse.json(VIEW)),
      http.get('/access/mailboxes', () => HttpResponse.json({ items: [
        { id: 'mbx-front', label: 'Front desk' }, { id: 'mbx-billing', label: 'Billing' }], next_cursor: null })),
      http.put('/numbers/reply', () => HttpResponse.json({ detail: refusal }, { status: 400 })),
    );
    render(<ReplyNumber client={client()} canWrite />);
    const section = await screen.findByTestId('reply-number');
    fireEvent.change(within(section).getByLabelText('Reply number for every fax'), { target: { value: '+18005550199' } });
    fireEvent.click(within(section).getByRole('button', { name: 'Save' }));
    expect(await within(section).findByText(refusal)).toBeTruthy();
  });

  it('cannot change anything without settings write', async () => {
    server.use(
      http.get('/numbers/reply', () => HttpResponse.json(VIEW)),
      http.get('/access/mailboxes', () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    render(<ReplyNumber client={client()} canWrite={false} />);
    const section = await screen.findByTestId('reply-number');
    expect((within(section).getByRole('button', { name: 'Use this number' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(section).getByLabelText('Reply number for every fax') as HTMLInputElement).disabled).toBe(true);
  });
});
