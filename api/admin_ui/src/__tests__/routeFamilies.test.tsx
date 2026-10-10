import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import RouteFamilies from '../components/RouteFamilies';
import type { RouteFamilies as Families, RouteTest } from '../api/routeFamilies';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

const problem = {
  id: 'p1', account: 'sip', account_label: 'Telnyx', transport: 't38' as const, phase: 't30', open: true,
  change_cause: 'settings' as const, since_text: '9 Oct 2:05 PM MDT', closed_text: null, closed_reason: null, retired: 0,
  destinations: 3,
  sentence: 'Since 9 Oct 2:05 PM MDT, fax over IP (T.38) by Telnyx has failed after the fax machine answered for 3 '
    + 'different numbers after its settings changed. Faxbot uses audio fax on this trunk meanwhile and does not count '
    + 'these failures against the numbers.',
  advice: 'To tell whether Telnyx or the numbers are at fault, run a 2-by-2 test.',
};

const test: RouteTest = {
  id: 't1', route_a: 'sip', route_b: 'sinch', route_a_label: 'Telnyx', route_b_label: 'Sinch',
  number_a: '+13035550111', number_b: '+13035550112', created_text: '10 Oct 9:00 AM MDT', verdict: 'unsent',
  sentence: 'Faxbot never sends a test fax by itself: send the 4 test faxes not sent yet when you are ready.',
  cells: [
    { cell: 'a1', account: 'sip', account_label: 'Telnyx', number: '+13035550111', state: 'not_sent', fax_id: null, sent_text: null },
    { cell: 'a2', account: 'sip', account_label: 'Telnyx', number: '+13035550112', state: 'not_sent', fax_id: null, sent_text: null },
    { cell: 'b1', account: 'sinch', account_label: 'Sinch', number: '+13035550111', state: 'not_sent', fax_id: null, sent_text: null },
    { cell: 'b2', account: 'sinch', account_label: 'Sinch', number: '+13035550112', state: 'not_sent', fax_id: null, sent_text: null },
  ],
};

const families = (overrides: Partial<Families> = {}): Families => ({
  incidents: [problem], tests: [test], upstreams: [],
  accounts: [{ key: 'sip', label: 'Telnyx', provider: 'sip' }, { key: 'sinch', label: 'Sinch', provider: 'sinch' }],
  numbers: ['+13035550111', '+13035550112'], ...overrides,
});

describe('Sending routes in Diagnostics', () => {
  it('shows an open route problem with the test that tells it apart, and nothing is sent by itself', async () => {
    const sends: string[] = [];
    server.use(http.get('/routing/families', () => HttpResponse.json(families())),
      http.post('/routing/families/tests/:id/send/:cell', ({ params }) => {
        sends.push(String(params.cell));
        return HttpResponse.json({ fax_id: 'f1', test, sentence: 'The test fax to +13035550112 by Sinch is on its way. '
          + 'Its result shows here when it ends.' });
      }));
    render(<RouteFamilies client={client()} />);
    expect(await screen.findByText(problem.sentence)).toBeTruthy();
    expect(screen.getByText(problem.advice)).toBeTruthy();
    expect(screen.getByText(test.sentence)).toBeTruthy();
    expect(sends).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Send the test fax by Sinch to +13035550112' }));
    expect(await screen.findByText(/The test fax to \+13035550112 by Sinch is on its way\./)).toBeTruthy();
    expect(sends).toEqual(['b2']);
  });

  it('closes a problem and shows a refusal in plain words', async () => {
    let closed = 0;
    server.use(http.get('/routing/families', () => HttpResponse.json(families())),
      http.post('/routing/families/:id/close', () => {
        closed += 1;
        return closed === 1
          ? HttpResponse.json({ ...problem, open: false, sentence: 'It ended: you closed it.' })
          : HttpResponse.json({ detail: 'This route problem has already ended.' }, { status: 409 });
      }));
    render(<RouteFamilies client={client()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Close this problem' }));
    expect(await screen.findByText('It ended: you closed it.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Close this problem' }));
    expect(await screen.findByText('This route problem has already ended.')).toBeTruthy();
  });

  it('says when nothing is wrong and when the routes cannot be read', async () => {
    server.use(http.get('/routing/families', () => HttpResponse.json(families({ incidents: [], tests: [] }))));
    render(<RouteFamilies client={client()} />);
    expect(await screen.findByText('Faxbot has found no problem shared by a whole sending route.')).toBeTruthy();
    expect(screen.getByText('No shared upstream is recorded.')).toBeTruthy();
    server.use(http.get('/routing/families', () => HttpResponse.json({ detail: 'nope' }, { status: 500 })));
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    expect(await screen.findByText('Faxbot could not read its sending routes. Check again in a moment.')).toBeTruthy();
  });

  it('plans a test only from your accounts and your own numbers', async () => {
    let planned: unknown = null;
    server.use(http.get('/routing/families', () => HttpResponse.json(families({ tests: [] }))),
      http.post('/routing/families/tests', async ({ request }) => {
        planned = await request.json();
        return HttpResponse.json(test);
      }));
    render(<RouteFamilies client={client()} />);
    const card = await screen.findByTestId('route-families');
    expect((within(card).getByRole('button', { name: 'Plan the test' }) as HTMLButtonElement).disabled).toBe(true);
    for (const [label, option] of [['First account', 'Telnyx'], ['Second account', 'Sinch'],
      ['First number of yours', '+13035550111'], ['Second number of yours', '+13035550112']]) {
      fireEvent.mouseDown(within(card).getByLabelText(label));
      fireEvent.click(await screen.findByRole('option', { name: option }));
    }
    fireEvent.click(within(card).getByRole('button', { name: 'Plan the test' }));
    await waitFor(() => expect(planned).toEqual({
      route_a: 'sip', route_b: 'sinch', number_a: '+13035550111', number_b: '+13035550112' }));
  });
});
