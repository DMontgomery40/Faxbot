import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import TelnyxNames from '../components/TelnyxNames';
import TollFreeApprovalPanel, { WHO_PAYS } from '../components/delivery/TollFreeApproval';
import { entryAction } from '../components/AuditLog';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

// -- caller-name lookup on the trunk page ----------------------------------------------------------------------

const ON = 'Telnyx looks up callers\' names on +1 555-555-0100, at $0.40 a month for each number. Faxbot never shows '
  + 'callers\' names, so turning it off changes nothing in Faxbot.';
const PRICE = { text: '$0.40 a month for each number', monthly: { currency: 'USD', amount: '0.40' },
  source_url: 'https://support.telnyx.com/en/articles/4366901-your-number-lookup-guide', read_on: '2026-10-07' };
const NAMES = {
  applies: true, checked_at: '2026-10-07T15:00:00Z', text: ON, price: PRICE, monthly_total: { currency: 'USD', amount: '0.40' },
  numbers: [
    { number: '+15555550100', display: '+1 555-555-0100', lookup: true, text: ON, can_turn_off: true },
    { number: '+15555550101', display: '+1 555-555-0101', lookup: false,
      text: 'Caller-name lookup is off for +1 555-555-0101.', can_turn_off: false },
  ],
};

describe('Caller-name lookup at Telnyx', () => {
  it('names the number where it is on, with its price and source, and turns it off for that number', async () => {
    const asked: string[] = [];
    const off = 'Caller-name lookup is off for all your trunk numbers, so Telnyx charges nothing for it.';
    server.use(
      http.get('/admin/sip/telnyx/names', () => HttpResponse.json(NAMES)),
      http.post('/admin/sip/telnyx/numbers/:number/name-lookup-off', ({ params }) => {
        asked.push(String(params.number));
        return HttpResponse.json({ ...NAMES, text: off, monthly_total: null, outcome: 'off',
          message: 'Telnyx now has caller-name lookup off for +1 555-555-0100.',
          numbers: NAMES.numbers.map((entry) => ({ ...entry, lookup: false, can_turn_off: false,
            text: `Caller-name lookup is off for ${entry.display}.` })) });
      }),
    );
    render(<TelnyxNames client={client()} />);
    const section = await screen.findByTestId('telnyx-names');
    expect(within(section).getByText(ON)).toBeTruthy();
    expect(within(section).getByRole('link', { name: "Telnyx's number lookup guide" }).getAttribute('href')).toBe(PRICE.source_url);
    fireEvent.click(within(section).getByRole('button', { name: 'Turn off name lookup for +1 555-555-0100' }));
    expect(await within(section).findByText('Telnyx now has caller-name lookup off for +1 555-555-0100.')).toBeTruthy();
    expect(within(section).getByText(off)).toBeTruthy();
    expect(within(section).queryByRole('button', { name: /Turn off name lookup/ })).toBeNull();
    expect(asked).toEqual(['+15555550100']);
  });

  it('says plainly when Telnyx refuses, and shows nothing off Telnyx', async () => {
    const refused = 'Telnyx did not let Faxbot change +1 555-555-0100, because the API key may not change numbers.';
    server.use(
      http.get('/admin/sip/telnyx/names', () => HttpResponse.json(NAMES)),
      http.post('/admin/sip/telnyx/numbers/:number/name-lookup-off', () => HttpResponse.json({ ...NAMES,
        outcome: 'refused', message: refused })),
    );
    render(<TelnyxNames client={client()} />);
    const section = await screen.findByTestId('telnyx-names');
    fireEvent.click(within(section).getByRole('button', { name: 'Turn off name lookup for +1 555-555-0100' }));
    expect(await within(section).findByText(refused)).toBeTruthy();
    server.use(http.get('/admin/sip/telnyx/names', () => HttpResponse.json({ ...NAMES, applies: false, numbers: [] })));
    const { container } = render(<TelnyxNames client={client()} refresh="again" />);
    await waitFor(() => expect(container.querySelector('[data-testid="telnyx-names"]')).toBeNull());
  });

  it('names both changes in the Audit log', () => {
    expect(entryAction({ operation: 'telnyx.caller_name_lookup', details: { shown: '+1 555-555-0100', result: 'turned_off' } }))
      .toBe('Turned off caller-name lookup at Telnyx for +1 555-555-0100');
    expect(entryAction({ operation: 'routing.toll_free_approval', details: { number: '+12025550123', action: 'approved' } }))
      .toBe("Recorded the recipient's approval of a toll-free number for +12025550123");
  });
});

// -- toll-free approval in Recipients → Details ------------------------------------------------------------------

const ROUTE = '/routing/destinations/:number/toll-free';

function row(action: 'noted' | 'approved' | 'withdrawn', extra: Record<string, unknown> = {}) {
  return { id: `${action}-1`, number: '+12025550123', alternate_number: '+18005550100', alternate_display: '+1 800-555-0100',
    action, approved_by: null, approved_on: null, evidence: null, recorded_by_name: 'Synthetic Owner',
    recorded_at: '2026-10-07T15:00:00', ...extra };
}

describe('Toll-free fax number in Recipients → Details', () => {
  it('puts a number on file, then records who approved it, when and the evidence', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    let history: Array<ReturnType<typeof row>> = [];
    server.use(
      http.get(ROUTE, () => HttpResponse.json({ number: '+12025550123', current: null, history: [], approved_alternate: null,
        sentence: null })),
      http.post(ROUTE, async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        bodies.push(body);
        const added = body.action === 'approved'
          ? row('approved', { approved_by: body.approved_by, approved_on: body.approved_on, evidence: body.evidence })
          : row('noted');
        history = [added, ...history];
        return HttpResponse.json({ number: '+12025550123', current: added, history,
          approved_alternate: body.action === 'approved' ? '+18005550100' : null,
          sentence: body.action === 'approved'
            ? 'Dana agreed on 3 October 2026. With this approval, Faxbot sends faxes for the recipient to +1 800-555-0100, '
              + 'and the recipient pays for those calls.'
            : '+1 800-555-0100 is on file but not approved.' });
      }),
    );
    render(<TollFreeApprovalPanel client={client()} number="+12025550123" canWrite />);
    const panel = await screen.findByTestId('toll-free-approval');
    expect(within(panel).getByText('No toll-free number is on file for this recipient.')).toBeTruthy();
    expect(within(panel).getByText(WHO_PAYS)).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: 'Add a toll-free number' }));
    fireEvent.change(within(panel).getByLabelText('Toll-free fax number'), { target: { value: '1-800-555-0100' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Put on file' }));
    expect(await within(panel).findByText('+1 800-555-0100 is on file but not approved.')).toBeTruthy();
    expect(within(panel).getByText('Not approved yet')).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: 'Record approval…' }));
    const save = within(panel).getByRole('button', { name: 'Record approval' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);  // who agreed is required
    fireEvent.change(within(panel).getByLabelText('Who at the recipient agreed'), { target: { value: 'Dana' } });
    fireEvent.change(within(panel).getByLabelText('Day they agreed'), { target: { value: '2026-10-03' } });
    fireEvent.change(within(panel).getByLabelText('Evidence'), { target: { value: 'Email from Dana' } });
    fireEvent.click(save);
    expect(await within(panel).findByText(/With this approval, Faxbot sends faxes for the recipient/)).toBeTruthy();
    expect(bodies).toEqual([
      { action: 'noted', alternate_number: '1-800-555-0100' },
      { action: 'approved', alternate_number: '+18005550100', approved_by: 'Dana', approved_on: '2026-10-03',
        evidence: 'Email from Dana' },
    ]);
    expect(within(panel).getByText('Evidence: Email from Dana')).toBeTruthy();
    expect(within(panel).getByRole('button', { name: 'Withdraw approval' })).toBeTruthy();
  });

  it('suggests a number the NPI registry lists, and still asks who approved it', async () => {
    server.use(http.get('/routing/destinations/:number/toll-free/suggestions', ({ request }) => {
      expect(new URL(request.url).searchParams.get('npi')).toBe('1234567893');
      return HttpResponse.json({ number: '+12025550123', sentence: 'NPPES lists a toll-free fax number for this provider. '
        + 'Check that it reaches the same intake, then record who at the recipient agreed before Faxbot uses it.',
      items: [{ source: 'NPPES', npi: '1234567893', name: 'SYNTHETIC CLINIC', address_purpose: 'mailing address',
        address: '1 Example Way, DENVER, CO', fax_number: '+18005550100', fax_display: '+1 800-555-0100',
        evidence: 'NPPES record NPI 1234567893, mailing address, read October 7, 2026' }] });
    }));
    render(<TollFreeApprovalPanel client={client()} number="+12025550123" canWrite />);
    const panel = await screen.findByTestId('toll-free-approval');
    fireEvent.change(within(panel).getByLabelText('NPI'), { target: { value: '1234567893' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Look up in NPPES' }));
    const suggestions = await within(panel).findByTestId('toll-free-suggestions');
    expect(within(suggestions).getByText(/^NPPES lists a toll-free fax number for this provider\./)).toBeTruthy();
    expect(within(suggestions).getByText('+1 800-555-0100, mailing address, 1 Example Way, DENVER, CO')).toBeTruthy();
    fireEvent.click(within(suggestions).getByRole('button', { name: 'Record approval…' }));
    const form = within(panel).getByTestId('toll-free-form');
    expect((within(form).getByLabelText('Toll-free fax number') as HTMLInputElement).value).toBe('+18005550100');
    // The registry is where the number is listed, not the recipient's agreement: the evidence stays the person's to write.
    expect((within(form).getByLabelText('Evidence') as HTMLTextAreaElement).value).toBe('');
    expect(within(form).getByText(/Listed in NPPES record NPI 1234567893/)).toBeTruthy();
  });

  it('shows an approval read-only without permission to change settings', async () => {
    server.use(http.get(ROUTE, () => HttpResponse.json({ number: '+12025550123',
      current: row('approved', { approved_by: 'Dana', approved_on: '2026-10-03', evidence: 'Email' }),
      history: [row('approved', { approved_by: 'Dana', approved_on: '2026-10-03' }), row('noted')],
      approved_alternate: '+18005550100', sentence: 'Dana agreed on 3 October 2026.' })));
    render(<TollFreeApprovalPanel client={client()} number="+12025550123" canWrite={false} />);
    const panel = await screen.findByTestId('toll-free-approval');
    expect(within(panel).getByText('Approved')).toBeTruthy();
    expect(within(panel).queryByRole('button')).toBeNull();
    expect(within(panel).getByText(/Approved \+1 800-555-0100, agreed by Dana on .*recorded by Synthetic Owner/)).toBeTruthy();
  });
});
