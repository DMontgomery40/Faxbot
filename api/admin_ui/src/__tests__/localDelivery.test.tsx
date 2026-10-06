// Faxes to this installation's own numbers: Sent details, the Send screen's real-call choice and Savings.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import JobsList, { BY_CALL_TEXT, routeText } from '../components/JobsList';
import Savings from '../components/delivery/Savings';
import { costAmount } from '../components/delivery/FaxCost';
import { emptySavings, server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
const JOB = 'd'.repeat(32);

function job(extra: Record<string, unknown> = {}) {
  return { id: JOB, to_number: '+17205550101', status: 'SUCCESS', backend: 'sip', pages: 2,
    created_at: '2026-10-06T12:00:00', updated_at: '2026-10-06T12:00:10', delivery_state: 'success',
    dispatch_mode: 'normal', delivery_version: 3, ...extra };
}

describe('a fax to one of your own numbers', () => {
  it('reads as delivered here with no call, and says why', async () => {
    const cost = { state: 'local' as const, summary: 'No call needed; it went straight into Received.', reported_cost: [],
      estimated_cost: [], route: 'local', routes: ['local'], route_reason: 'own_number',
      route_explanation: 'This is one of your own fax numbers, so the fax went straight into Received without a phone call.' };
    expect(routeText('sip', cost)).toBe('This Faxbot');
    expect(costAmount(cost)).toBe('No call');
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job()] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job())),
      http.get('/routing/fax-costs', () => HttpResponse.json({ costs: { [JOB]: cost } })),
      http.get(`/routing/faxes/${JOB}/cost`, () => HttpResponse.json(cost)),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+17205550101'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect(await within(dialog).findByText('This Faxbot')).toBeTruthy();
    expect((await within(dialog).findByTestId('job-route-reason')).textContent).toBe(cost.route_explanation);
    expect(await within(dialog).findByText('No call needed; it went straight into Received.')).toBeTruthy();
    expect(within(dialog).queryByTestId('job-by-call')).toBeNull();
  });

  it('says when you asked for a real call through your carrier', async () => {
    server.use(
      http.get('/admin/fax-jobs', () => HttpResponse.json({ total: 1, jobs: [job()] })),
      http.get(`/admin/fax-jobs/${JOB}`, () => HttpResponse.json(job({ send_by_call: true }))),
    );
    render(<JobsList client={client()} />);
    fireEvent.click(await screen.findByText('+17205550101'));
    const dialog = await screen.findByRole('dialog', { name: 'Fax details' });
    expect((await within(dialog).findByTestId('job-by-call')).textContent).toBe(BY_CALL_TEXT);
  });

  it('asks the server for a real call only when the sender chose it', async () => {
    server.use(http.post('/fax', () => HttpResponse.json({ id: JOB, status: 'queued' }, { status: 202 })));
    const appended = vi.spyOn(FormData.prototype, 'append');
    const file = new File(['%PDF-1.4'], 'page.pdf', { type: 'application/pdf' });
    await client().sendFax('+17205550101', file, { byCall: true });
    expect(appended.mock.calls.filter(([name]) => name === 'send_by_call')).toEqual([['send_by_call', 'true']]);
    appended.mockClear();
    await client().sendFax('+17205550101', file);
    expect(appended.mock.calls.some(([name]) => name === 'send_by_call')).toBe(false);
    appended.mockRestore();
  });

  it('counts the calls it avoided in Savings', async () => {
    const savings = emptySavings();
    savings.own_numbers.sentence = '2 faxes to your own numbers went straight into Received, so 2 phone calls were not needed.';
    server.use(http.get('/routing/savings', () => HttpResponse.json(savings)));
    render(<Savings client={client()} />);
    expect((await screen.findByTestId('savings-own')).textContent).toContain('2 phone calls were not needed.');
  });
});
