import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient, { AdminAPIError } from '../api/client';
import SetupPacks from '../components/SetupPacks';
import SetupWizard from '../components/SetupWizard';
import type { ApplyResult, Plan, PlanItem, SetupPacksApi } from '../components/SetupPacksApi';
import { server } from '../test/server';
import { receipt, settingsFixture } from '../test/settingsFixture';

const REVISION = 'a'.repeat(64);

function item(key: string, changes: Partial<PlanItem> = {}): PlanItem {
  return {
    key, pack: 'cost', kind: 'rule', title: key, sentence: `${key} explained.`, sources: [], saving: null,
    selected: true, blocked: null, scope: 'organization', applies_to: null, link: null, cli: null, ...changes,
  };
}

const COUNTRY = item('cost.country.GB.sinch', {
  title: 'Numbers in the United Kingdom go by Sinch',
  sentence: 'Faxes to +44 numbers cost about $0.05 less each through Sinch over the last 30 days.',
  sources: [{ name: 'Costs → Recommendations', detail: '9 delivered faxes to 2 numbers' }],
  saving: { amount: '0.12', currency: 'USD', faxes: 2, estimate: true },
});
const HEADER = item('compliance.header', {
  pack: 'compliance', kind: 'setting', title: 'Print your business name at the top of each page',
  sentence: 'US fax rules (47 CFR 68.318(d)) ask that each page shows the business sending it. A suggestion, not legal advice.',
  applies_to: ['box-us'],
});
const INVITE = item('partners.invite.+12025550123', {
  pack: 'partners', kind: 'step', title: 'Invite County Clinic to be a direct partner', selected: false,
  link: 'recipients/partners', sentence: 'You sent 5 faxes to this number.',
});
const BLOCKED = item('reliability.fallback.+12025550123', {
  pack: 'reliability', title: 'Faxes to County Clinic try SignalWire first', selected: false,
  blocked: 'You have unpublished changes to the rules of the organization.',
});
const ALREADY = item('cost.fax-friendly', { kind: 'in_effect', title: 'Shaded areas are lightened where it saves time', selected: false });

function plan(changes: Partial<Plan> = {}): Plan {
  return {
    number: 4, revision: REVISION, created_at: '2026-10-08T15:00:00', actor_name: 'Ada Admin',
    context: { organization_name: 'Example Health', country: 'US', mailboxes: {} },
    packs: [
      { key: 'cost', title: 'Costs', sentence: 'Rules and settings that send your faxes the cheaper way.',
        items: [COUNTRY, ALREADY], saving: { amount: '0.12', currency: 'USD' } },
      { key: 'partners', title: 'Partners', sentence: 'Recipients who run Faxbot.', items: [INVITE], saving: null },
      { key: 'receiving', title: 'Receiving', sentence: 'Where received faxes go.', items: [], saving: null },
      { key: 'reliability', title: 'Reliability', sentence: 'Numbers where your first route keeps failing.',
        items: [BLOCKED], saving: null },
      { key: 'compliance', title: 'Compliance basics', sentence: 'Suggestions only.', items: [HEADER], saving: null },
    ],
    missing: [
      { key: 'retention', sentence: 'Choose how long Faxbot keeps the documents you send.', owner: 'you',
        operation: 'Compliance basics: keeping sent documents', status: 'warning', scope: 'organization',
        link: 'system/storage' },
      { key: 'reviewed-rules.GB', sentence: 'Faxbot has no reviewed fax rules for the United Kingdom yet.',
        owner: 'faxbot', operation: 'Compliance basics', status: 'warning', scope: 'box-gb', link: null },
      { key: 'draft.organization', sentence: 'Your rules for the organization have unpublished changes.',
        owner: 'you', operation: 'Rules in this plan', status: 'blocking', scope: 'organization', link: 'providers/rules' },
    ],
    mailboxes: [
      { id: 'box-us', name: 'Denver', country: 'US', country_source: 'stated', items: ['compliance.header'], missing: [],
        choices: [{ label: 'Country', value: 'the United States', source: 'You stated it' },
          { label: 'Header line', value: 'Faxbot', source: 'This installation' }] },
      { id: 'box-gb', name: 'Leeds', country: 'GB', country_source: 'stated', items: [], missing: ['reviewed-rules.GB'],
        choices: [{ label: 'Country', value: 'the United Kingdom', source: 'You stated it' }] },
    ],
    workflows: [],
    checks: { organization: { warnings: [], replay: 'None of your last 12 faxes would have gone another way.' } },
    applications: [],
    ...changes,
  };
}

function memory(first: Plan | null = null) {
  const calls: Array<{ name: string; args: unknown[] }> = [];
  const api: SetupPacksApi = {
    latest: vi.fn(async () => ({ plan: first, mailboxes: [
      { id: 'box-us', name: 'Denver', numbers: ['+13035550100'], numbers_country: 'US' },
      { id: 'box-gb', name: 'Leeds', numbers: [], numbers_country: null }] })),
    preview: vi.fn(async (context) => { calls.push({ name: 'preview', args: [context] }); return plan({ context }); }),
    apply: vi.fn(async (current, items) => {
      calls.push({ name: 'apply', args: [current.number, current.revision, items] });
      const done = plan({ applications: [{ outcome: 'applied', items, steps: [], restart_required: false,
        actor_name: 'Ada Admin', created_at: '2026-10-08T15:05:00' }] });
      return { outcome: 'applied', items, restart_required: false, sentence: `Applied ${items.length} suggestions.`,
        steps: [{ part: 'settings', outcome: 'done', sentence: 'Settings saved and in use.' }], plan: done } as ApplyResult;
    }),
  };
  return { api, calls };
}

async function choose(label: string, option: string) {
  fireEvent.mouseDown(screen.getByLabelText(label));
  fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: option }));
}

describe('Setup → Suggested Packs', () => {
  it('previews from what the administrator states, and never fills a country in', async () => {
    const { api, calls } = memory();
    render(<SetupPacks api={api} countries={['US', 'GB']} />);
    await screen.findByText('Describe your organization');
    expect(screen.getByText('Its numbers are in United States.')).toBeTruthy();
    expect(screen.getByLabelText('Country Denver works in').textContent).toBe('Not stated');
    fireEvent.change(screen.getByLabelText('Business name'), { target: { value: 'Example Health' } });
    await choose('Country Denver works in', 'United States');
    await choose('Country Leeds works in', 'United Kingdom');
    fireEvent.click(screen.getByRole('button', { name: 'Preview suggestions' }));
    await screen.findByText('Numbers in the United Kingdom go by Sinch');
    expect(calls[0]).toEqual({ name: 'preview', args: [{ organization_name: 'Example Health', country: '',
      mailboxes: { 'box-us': { country: 'US' }, 'box-gb': { country: 'GB' } } }] });
    // Each suggestion with its kind, explanation, sources and saving; nothing internal is shown.
    expect(screen.getByText('About $0.12 a month (estimate)')).toBeTruthy();
    expect(screen.getByText('From Costs → Recommendations: 9 delivered faxes to 2 numbers')).toBeTruthy();
    expect(screen.getByText('The chosen suggestions save about $0.12 a month (estimate).')).toBeTruthy();
    expect(screen.getByText('Already on')).toBeTruthy();
    expect(screen.getByText('Checked against your recent faxes: None of your last 12 faxes would have gone another way.')).toBeTruthy();
    expect(document.body.textContent).not.toContain(REVISION);
    expect(document.body.textContent).not.toMatch(/setup-[0-9a-f]{12}|box-us|cost\.country/);
  });

  it('shows what is missing, whose choice it is, and each mailbox on its own', async () => {
    const { api } = memory(plan());
    const navigate = vi.fn();
    render(<SetupPacks api={api} countries={['US', 'GB']} onNavigate={navigate} />);
    await screen.findByText('What’s missing');
    const retention = screen.getByTestId('setup-missing-retention');
    expect(within(retention).getByText('Your choice')).toBeTruthy();
    expect(within(retention).getByText('Affects: Compliance basics: keeping sent documents')).toBeTruthy();
    expect(within(screen.getByTestId('setup-missing-reviewed-rules.GB')).getByText('Not in Faxbot yet')).toBeTruthy();
    expect(within(screen.getByTestId('setup-missing-draft.organization')).getByText('Holds back: Rules in this plan')).toBeTruthy();
    fireEvent.click(within(retention).getByRole('button', { name: 'Open the page' }));
    expect(navigate).toHaveBeenCalledWith('system/storage');
    const denver = screen.getByRole('table', { name: 'Denver settings' });
    expect(within(denver).getByText('This installation')).toBeTruthy();
    expect(screen.getByRole('table', { name: 'Leeds settings' })).toBeTruthy();
    // A step stays with you: no checkbox, and a link to its page.
    const invite = screen.getByTestId('setup-item-partners.invite.+12025550123');
    expect(within(invite).queryByRole('checkbox')).toBeNull();
    fireEvent.click(within(invite).getByRole('button', { name: 'Open the page' }));
    expect(navigate).toHaveBeenCalledWith('recipients/partners');
    // A blocked suggestion says why and can't be chosen.
    const blocked = screen.getByTestId('setup-item-reliability.fallback.+12025550123');
    expect(within(blocked).queryByRole('checkbox')).toBeNull();
    expect(within(blocked).getByText('You have unpublished changes to the rules of the organization.')).toBeTruthy();
  });

  it('applies exactly the chosen suggestions with the plan it previewed', async () => {
    const { api, calls } = memory(plan());
    render(<SetupPacks api={api} countries={['US']} />);
    await screen.findByRole('button', { name: 'Apply 2 chosen suggestions' });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Numbers in the United Kingdom go by Sinch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply 1 chosen suggestion' }));
    await screen.findByText('Applied 1 suggestions.');
    expect(calls).toEqual([{ name: 'apply', args: [4, REVISION, ['compliance.header']] }]);
    expect(screen.getByText('Settings saved and in use.')).toBeTruthy();
    const header = screen.getByTestId('setup-item-compliance.header');
    expect(within(header).getByText('Applied')).toBeTruthy();
    expect(within(header).queryByRole('checkbox')).toBeNull();
  });

  it('offers a new preview when the plan is out of date', async () => {
    const { api } = memory(plan());
    api.apply = vi.fn(async () => {
      throw new AdminAPIError(409, 'Conflict', 'Your sending rules changed since this preview. Preview again to see the plan against your current rules.');
    });
    render(<SetupPacks api={api} countries={['US']} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Apply 2 chosen suggestions' }));
    await screen.findByText(/Your sending rules changed since this preview/);
    fireEvent.click(screen.getAllByRole('button', { name: 'Preview again' })[0]);
    await waitFor(() => expect(api.preview).toHaveBeenCalled());
  });

  it('talks to the setup plan routes with the plan revision', async () => {
    const bodies: unknown[] = [];
    server.use(
      http.get('/setup/plans/latest', () => HttpResponse.json({ plan: plan(), mailboxes: [] })),
      http.post('/setup/plans/4/apply', async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ outcome: 'applied', items: ['compliance.header'], steps: [], restart_required: false,
          sentence: 'Applied 1 suggestion.', plan: plan() });
      }),
    );
    render(<SetupPacks client={new AdminAPIClient({ kind: 'key', key: 'synthetic-key' })} countries={['US']} />);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Numbers in the United Kingdom go by Sinch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply 1 chosen suggestion' }));
    await screen.findByText('Applied 1 suggestion.');
    expect(bodies).toEqual([{ expected_revision: REVISION, items: ['compliance.header'] }]);
  });

  it('is a step of the Setup wizard, before Finish', async () => {
    const data = settingsFixture();
    server.use(
      http.get('/admin/settings', () => HttpResponse.json(data)),
      http.get('/plugins', () => HttpResponse.json({ items: [] })),
      http.get('/admin/sip/presets', () => HttpResponse.json({ presets: [] })),
      http.get('/admin/sip/status', () => HttpResponse.json({ configured: false })),
      http.get('/admin/sip/network', () => HttpResponse.json({ applies: false, checked: false })),
      http.put('/admin/settings', () => HttpResponse.json(receipt('rev-1', false))),
      http.get('/setup/plans/latest', () => HttpResponse.json({ plan: null, mailboxes: [] })),
    );
    render(<SetupWizard client={new AdminAPIClient({ kind: 'key', key: 'synthetic-key' })} />);
    await screen.findByText('Choose Providers', { selector: 'h6' });
    const labels = Array.from(document.querySelectorAll('.MuiStepLabel-label')).map((node) => node.textContent);
    expect(labels).toEqual(['Choose Providers', 'Connect Providers', 'Security', 'Delivery Options', 'Suggested Packs', 'Finish']);
    for (let step = 0; step < 4; step += 1) {
      fireEvent.click(screen.getByRole('button', { name: 'Next' }));
      await waitFor(() => expect(screen.queryByText('Working…')).toBeNull());
    }
    await screen.findByText('Suggested Packs', { selector: 'h6' });
    expect(screen.getByRole('button', { name: 'Preview suggestions' })).toBeTruthy();
  });
});
