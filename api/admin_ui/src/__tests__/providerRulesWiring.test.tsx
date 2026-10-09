// The provider-rules screens wired into the console: the API client's transport, Numbers' receiving
// options, Sent's held faxes, Overview's card, Send a fax's fields and Recommendations' Add as rule.
// These go through the real AdminAPIClient and the shared test server.
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import { ORGANIZATION, rulesApiFor } from '../components/ProviderRulesApi';
import ResourceAccess from '../components/ResourceAccess';
import JobsList from '../components/JobsList';
import Dashboard from '../components/Dashboard';
import SendFax from '../components/SendFax';
import SendingRecommendations from '../components/delivery/SendingRecommendations';
import { backend, server } from '../test/server';

const keyClient = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

function choose(container: HTMLElement, name: string | RegExp, option: string) {
  fireEvent.mouseDown(within(container).getByRole('combobox', { name }));
  fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: option }));
}

const hold = {
  id: 'h-1', job_id: 'job-1', kind: 'approval', to_number: '+15550100001', pages: 24, sender_name: 'Nia New',
  requested_at: '2026-10-07T16:00:00', until: null, reason: 'Waiting for approval: the rule ‘Faxes over 20 pages need approval’ matched.',
  can_decide: true, version: 2,
};

describe('the API client carries the rules requests', () => {
  it('sends the key and the JSON body, reads an empty 204, and keeps the server’s sentence on a conflict', async () => {
    const seen: Array<{ path: string; body: unknown; key: string | null }> = [];
    server.use(
      http.put('/routing/rules/draft', async ({ request }) => {
        const url = new URL(request.url);
        seen.push({ path: url.pathname + url.search, body: await request.json(), key: request.headers.get('X-API-Key') });
        return HttpResponse.json({ document: { format: 1 }, version: 1, base_revision: null, actor_name: null,
          updated_at: '2026-10-07T12:00:00', check: null });
      }),
      http.delete('/routing/rules/draft', () => new HttpResponse(null, { status: 204 })),
      http.post('/routing/rules/publish', () => HttpResponse.json(
        { detail: 'Someone published other rules meanwhile. Reload them and check again.' }, { status: 409 })),
    );
    const client = keyClient();
    const api = rulesApiFor(client);
    expect(rulesApiFor(client)).toBe(api);
    expect((await api.saveDraft(ORGANIZATION, { format: 1 }, 0)).version).toBe(1);
    expect(seen).toEqual([{ path: '/routing/rules/draft?scope=organization', body: { document: { format: 1 }, expected_version: 0 },
      key: 'synthetic-key' }]);
    await expect(api.discardDraft(ORGANIZATION)).resolves.toBeUndefined();
    await expect(api.publish(ORGANIZATION, 1, 1, 'Late')).rejects.toMatchObject({
      status: 409, detail: 'Someone published other rules meanwhile. Reload them and check again.' });
  });
});

describe('Numbers: receiving options on a number rule', () => {
  it('saves only the options chosen, shows the rule as a sentence and offers Try a received fax', async () => {
    const admin = backend.state.principals.get('p_admin')!;
    await AdminAPIClient.login('admin', 'correct horse');
    const client = new AdminAPIClient({ kind: 'session', csrf: null });
    const me = await client.me();
    expect(admin.permissions).toContain('mailboxes:manage');
    render(<ResourceAccess client={client} me={me} section="numbers" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add number' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Fax number/), { target: { value: '+15550100009' } });
    fireEvent.change(within(dialog).getByLabelText('Mailbox'), { target: { value: 'mbx_main' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'More choices: account, sender, times, email, urgency' }));
    fireEvent.click(within(dialog).getByLabelText('Mark these faxes urgent'));
    fireEvent.change(within(dialog).getByLabelText('Keep for (days)'), { target: { value: '30' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add number' }));
    expect(await screen.findByText('Faxes to +15550100009 go to Main line, marked urgent, kept for 30 days.')).toBeTruthy();
    const saved = [...backend.state.rules.values()].find((rule) => rule.to_number === '+15550100009')!;
    expect(saved).toMatchObject({ mailbox_id: 'mbx_main', urgent: true, keep_days: 30 });
    expect(Object.keys(saved).sort()).toEqual(['id', 'keep_days', 'mailbox_id', 'to_number', 'urgent', 'version']);
    expect(await screen.findByRole('region', { name: 'Try a received fax' })).toBeTruthy();
  });
});

describe('Faxes → Sent: held faxes', () => {
  it('approves a held fax with the version it read', async () => {
    const approved: unknown[] = [];
    server.use(
      http.get('/routing/holds', () => HttpResponse.json({ holds: [hold] })),
      http.post('/routing/holds/:id/approve', async ({ request }) => {
        approved.push(await request.json());
        return HttpResponse.json({ ...hold, version: 3, sentence: 'Approved. The fax to +15550100001 goes by Telnyx.' });
      }),
    );
    render(<JobsList client={keyClient()} canApprove onNavigate={() => undefined} />);
    expect(await screen.findByText(hold.reason)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Approve the fax to +15550100001' }));
    expect(await screen.findByText('Approved. The fax to +15550100001 goes by Telnyx.')).toBeTruthy();
    expect(approved).toEqual([{ version: 2 }]);
  });

  it('shows nothing to someone who may not see held faxes', async () => {
    server.use(http.get('/routing/holds', () => HttpResponse.json({ detail: 'This operation is not permitted.' }, { status: 403 })));
    render(<JobsList client={keyClient()} />);
    await screen.findByRole('heading', { name: 'Sent' });
    await waitFor(() => expect(screen.queryByText('You do not have permission to do this.')).toBeNull());
    expect(screen.queryByRole('region', { name: 'Faxes waiting for you' })).toBeNull();
  });
});

describe('Overview: faxes waiting for you', () => {
  it('shows the held faxes and opens Sent', async () => {
    server.use(http.get('/routing/holds', () => HttpResponse.json({ holds: [hold] })));
    const navigate = vi.fn();
    render(<Dashboard client={keyClient()} onNavigate={navigate} />);
    expect(await screen.findByText('1 fax is waiting for you')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open Sent' }));
    expect(navigate).toHaveBeenCalledWith('faxes/sent');
  });
});

describe('Send a fax: mailbox and labels for rules', () => {
  it('adds the chosen mailbox and labels to the fax, and nothing when none is offered', async () => {
    const append = vi.spyOn(FormData.prototype, 'append');
    server.use(http.post('/fax', () => HttpResponse.json({ id: 'c'.repeat(32), status: 'queued', delivery_state: 'ready',
      to: '+15551234567' }, { status: 202 })));
    try {
      render(<SendFax client={keyClient()} config={{ fax_disabled: false, max_file_size_mb: 10 } as never} configLoading={false}
        configError={null} sendChoices={{ mailboxes: [{ id: 'm-leeds', label: 'Leeds intake' }], labels: ['legal', 'clinical'] }} />);
      choose(document.body, 'Send from mailbox', 'Leeds intake');
      fireEvent.click(screen.getByLabelText('legal'));
      expect(screen.queryByRole('combobox', { name: 'Workflow' })).toBeNull();
      fireEvent.change(screen.getByRole('textbox', { name: /Destination Number/ }), { target: { value: '+15551234567' } });
      fireEvent.change(window.document.querySelector('input[type="file"]') as HTMLInputElement,
        { target: { files: [new File(['%PDF-1.4 referral'], 'referral.pdf', { type: 'application/pdf' })] } });
      fireEvent.click(screen.getByRole('button', { name: 'Send Fax' }));
      expect(await screen.findByText('Fax queued for +15551234567.')).toBeTruthy();
      const fields = append.mock.calls.map(([name, value]) => [name, value]);
      expect(fields).toContainEqual(['mailbox', 'm-leeds']);
      expect(fields).toContainEqual(['labels', 'legal']);
      expect(fields.some(([name]) => name === 'workflow')).toBe(false);
    } finally {
      append.mockRestore();
    }
  });
});

describe('Costs → Recommendations: Add as rule', () => {
  it('puts a recommended rule in the organization’s draft without publishing', async () => {
    const drafts: Array<{ document: { routes: Array<{ name: string }> } }> = [];
    server.use(
      http.get('/routing/recommendations/sending', () => HttpResponse.json({ window_days: 30, min_delivered: 3, empty_sentence: '',
        items: [{ number: '+442071234567', display_name: null, version: 1, preferred_route: null, chosen_by_you: false,
          kind: 'cheaper_route', current_label: 'Telnyx', current: null,
          suggested: { route: 'sinch-uk', label: 'Sinch (UK)', delivered: 38, attempts: 40, cost_text: '$0.031', basis_text: null },
          saving_per_fax: { currency: 'USD', amount: '0.031' }, sentence: 'Faxes to +44 numbers cost less through Sinch (UK).',
          rule_suggestion: { name: 'Faxes to +44 numbers go through Sinch (UK)', when: { destination: { prefixes: ['+44'] } },
            then: { use: 'sinch-uk' } } }] })),
      http.put('/routing/rules/draft', async ({ request }) => {
        drafts.push(await request.json() as never);
        return HttpResponse.json({ document: {}, version: 1, base_revision: null, actor_name: null, updated_at: '2026-10-07T12:00:00', check: null });
      }),
    );
    render(<SendingRecommendations client={keyClient()} canWrite onNavigate={() => undefined} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add as rule' }));
    expect(await screen.findByText('“Faxes to +44 numbers go through Sinch (UK)” is in your draft on Providers → Rules. It takes effect when you publish it.')).toBeTruthy();
    expect(drafts.map((draft) => draft.document.routes.map((rule) => rule.name))).toEqual([['Faxes to +44 numbers go through Sinch (UK)']]);
  });

  it('offers one rule for a whole country where the same account was cheaper for several numbers', async () => {
    const drafts: Array<{ document: { routes: Array<{ name: string; when: unknown }> } }> = [];
    const item = (number: string) => ({ number, display_name: null, version: 1, preferred_route: null, chosen_by_you: false,
      kind: 'cheaper_route', current_label: 'Telnyx', current: null,
      suggested: { route: 'sinch', label: 'Sinch', delivered: 19, attempts: 19, cost_text: '$0.03', basis_text: null },
      saving_per_fax: { currency: 'USD', amount: '0.031' }, sentence: `Sinch cost less for ${number}.`, rule_suggestion: null });
    server.use(
      http.get('/routing/recommendations/sending', () => HttpResponse.json({ window_days: 30, min_delivered: 3, empty_sentence: '',
        items: [item('+442071234567'), item('+441614960000')],
        country_rules: [{ country: 'GB', route: 'sinch', numbers: 2, delivered: 38, saving_per_fax: { currency: 'USD', amount: '0.031' },
          sentence: 'Faxes to +44 numbers cost about $0.031 less each through Sinch over the last 30 days (38 delivered faxes to 2 numbers). Add as a rule?',
          rule_suggestion: { name: 'Numbers in the United Kingdom go by Sinch', when: { destination: { countries: ['GB'] } },
            then: { use: 'sinch' } } }] })),
      http.put('/routing/rules/draft', async ({ request }) => {
        drafts.push(await request.json() as never);
        return HttpResponse.json({ document: {}, version: 1, base_revision: null, actor_name: null, updated_at: '2026-10-07T12:00:00', check: null });
      }),
    );
    render(<SendingRecommendations client={keyClient()} canWrite onNavigate={() => undefined} />);
    expect(await screen.findByText(/Faxes to \+44 numbers cost about \$0\.031 less each through Sinch/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add as rule' }));
    expect(await screen.findByText('“Numbers in the United Kingdom go by Sinch” is in your draft on Providers → Rules. It takes effect when you publish it.')).toBeTruthy();
    expect(drafts[0].document.routes[0].when).toEqual({ destination: { countries: ['GB'] } });
  });
});
