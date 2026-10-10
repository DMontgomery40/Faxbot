import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import TestLines from '../components/TestLines';
import { server } from '../test/server';

const GB_GUARD = { allowed: false, class: 'country:GB', class_label: 'United Kingdom',
  class_text: 'numbers in the United Kingdom', fenced: false,
  sentence: 'Faxbot has not sent to numbers in the United Kingdom before, so it does not dial this line until you allow that country.' };
const OPEN = { allowed: true, class: 'national_toll_free', class_label: 'Toll-free numbers in your country',
  class_text: 'toll-free numbers in your country', fenced: false, sentence: null };

function line(id: string, operator: string, number: string, kind: string, guard: object, extra: object = {}) {
  return { id, number, country: 'US', operator, kind, shows: `${operator} does its test.`, source_url: `https://example.com/${id}`,
    read_on: '2026-10-09', invitation: null, invitation_en: null, reply_minutes: kind === 'public' ? null : 20,
    max_pages: null, public_page: null, note: null, faxbeep_receipt: false, guard, ...extra };
}

const REPLY = { caller_id: '+13035550101', header: '+13035550101', reaches: true,
  sentence: 'Replies come back to +13035550101, the number your faxes show, and Faxbot receives faxes on it.' };

describe('Public test lines in Diagnostics', () => {
  it('asks before a public page, then allows a country only through the guard with your confirmation', async () => {
    const calls: string[] = [];
    let allowed = false;
    server.use(
      http.get('/diagnostics/test-lines', () => HttpResponse.json({ reply: REPLY, sends: [], lines: [
        line('faxbeep-gb', 'Faxbeep', '+442038089463', 'public', allowed ? { ...GB_GUARD, allowed: true, sentence: null } : GB_GUARD),
        line('hp-us', "HP's fax test service", '+18884732963', 'reply', OPEN,
          { note: "HP's support page invites a one-page test fax; its exact words were not kept." }),
      ] })),
      http.post('/diagnostics/test-lines/faxbeep-gb/send', () => {
        calls.push('send');
        return HttpResponse.json(allowed
          ? { sent: true, sentence: 'The test fax to Faxbeep is on its way. Its result shows here when the call ends.', send: null }
          : { sent: false, sentence: GB_GUARD.sentence, needs_allow: GB_GUARD });
      }),
      http.put('/routing/dialing/country%3AGB', async ({ request }) => {
        calls.push(`allow ${JSON.stringify(await request.json())}`);
        allowed = true;
        return HttpResponse.json({ sentence: 'Faxbot may dial numbers in the United Kingdom.', classes: [], countries: [] });
      }),
    );
    render(<TestLines client={new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' })} />);
    const section = await screen.findByTestId('test-lines');
    expect(within(section).getByTestId('test-lines-reply').textContent).toContain('Faxbot receives faxes on it');
    expect(within(section).getByText(GB_GUARD.sentence)).toBeTruthy();
    expect(within(section).getByText(/exact words were not kept/)).toBeTruthy();
    fireEvent.click(within(section).getByRole('button', { name: 'Send a test fax to Faxbeep, +442038089463' }));
    // A public page: nothing is sent until you confirm.
    expect(await screen.findByText(/shows every fax it receives on a public web page/)).toBeTruthy();
    expect(calls).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Send the test fax' }));
    // The guard holds it: you are asked before Faxbot may dial the United Kingdom.
    expect(await screen.findByText('Allow calls to United Kingdom?')).toBeTruthy();
    expect(calls).toEqual(['send']);
    fireEvent.click(screen.getByRole('button', { name: 'Allow and send' }));
    expect(await screen.findByText('The test fax to Faxbeep is on its way. Its result shows here when the call ends.')).toBeTruthy();
    expect(calls).toEqual(['send', 'allow {"state":"allowed"}', 'send']);
  });

  it('finds a Faxbeep receipt on request and lets you mark a reply that came from another number', async () => {
    const marked: unknown[] = [];
    const send = { id: 's1', line_id: 'faxbeep-us', operator: 'Faxbeep', number: '+19725329272', fax_id: 'f1',
      sent_at_text: '10 Oct 9:00 AM MDT', actor_name: 'Dana', fax_state: 'sent', fax_sentence: 'The test fax went through.',
      reply: null, public_page: 'https://faxbeep.com/', receipt: 'faxbeep' };
    const hp = { ...send, id: 's2', line_id: 'hp-us', operator: "HP's fax test service", number: '+18884732963', receipt: null,
      public_page: null, reply: { state: 'possible', inbound_id: null, sentence: 'A fax arrived on +13035550101 from another number while Faxbot waited for HP\'s fax test service. If it is the reply, mark it as the test reply.',
        candidates: [{ inbound_id: 'in1', from_number: '+18005550100', pages: 1, received_at_text: '10 Oct 9:06 AM MDT' }] } };
    server.use(
      http.get('/diagnostics/test-lines', () => HttpResponse.json({ reply: REPLY, lines: [], sends: [send, hp] })),
      http.get('/diagnostics/test-lines/sends/s1/receipt', () => HttpResponse.json({
        url: 'https://faxbeep.com/faxtest/fax_8c93f66b', list_url: 'https://faxbeep.com/',
        sentence: 'Faxbeep shows your test page on its public page.' })),
      http.post('/diagnostics/test-lines/sends/s2/reply', async ({ request }) => {
        marked.push(await request.json());
        return HttpResponse.json({ ...hp, reply: { state: 'replied', inbound_id: 'in1', candidates: [], sentence: 'Done.' } });
      }),
    );
    render(<TestLines client={new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' })} />);
    const results = await screen.findByTestId('test-line-results');
    fireEvent.click(within(results).getByRole('button', { name: 'Find it on Faxbeep' }));
    const link = await within(results).findByRole('link', { name: 'Open your test page' });
    expect(link.getAttribute('href')).toBe('https://faxbeep.com/faxtest/fax_8c93f66b');
    expect(within(results).getByText(/Received 10 Oct 9:06 AM MDT from \+18005550100\./)).toBeTruthy();
    fireEvent.click(within(results).getByRole('button', { name: 'Mark as the test reply' }));
    await waitFor(() => expect(marked).toEqual([{ inbound_id: 'in1' }]));
  });

  it('says so when the list cannot be read', async () => {
    server.use(http.get('/diagnostics/test-lines', () => HttpResponse.json({ detail: 'down' }, { status: 503 })));
    render(<TestLines client={new AdminAPIClient({ kind: 'key', key: 'synthetic-console-key' })} />);
    expect(await screen.findByTestId('test-lines-unread')).toBeTruthy();
  });
});
