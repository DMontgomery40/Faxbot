// An in-memory provider-rules server for the console's tests. It answers the requests of
// ProviderRulesApi.ts the way the design describes the API: one draft per scope with a version, 409
// for a stale draft or publish, holds with versions, and accounts with a configuration generation.
// Every request is recorded so a test can check exactly what a screen sent.
import { AdminAPIError } from '../api/client';
import type {
  AccountsState, ApiRequest, Choices, Hold, RevisionDetail, RulesDocument, RulesState,
} from '../components/ProviderRulesApi';
import { rulesApi } from '../components/ProviderRulesApi';

type Json = Record<string, any>;

export const CHOICES: Choices = {
  accounts: [
    { key: 'sip', label: 'Telnyx', provider: 'sip', sends: true, enabled: true, site: null },
    { key: 'sinch-uk', label: 'Sinch (UK)', provider: 'sinch', sends: true, enabled: true, site: 'leeds' },
    { key: 'humblefax', label: 'HumbleFax', provider: 'humblefax', sends: true, enabled: true, site: null },
  ],
  people: [{ id: 'p-ada', name: 'Ada Admin', kind: 'user' }],
  keys: [{ id: 'k-scan', name: 'Scanner key' }],
  groups: [{ id: 'g-legal', name: 'Legal' }],
  mailboxes: [{ id: 'm-leeds', name: 'Leeds intake' }],
};

export function organizationDocument(): RulesDocument {
  return {
    format: 1,
    lists: { 'uk-clinics': { name: 'UK clinics', numbers: ['+441782684953'], prefixes: ['+4420'] }, labels: ['legal'] },
    regions: { north: { name: 'Northern England', prefixes: ['+44113'] } },
    sites: [{ key: 'leeds', name: 'Leeds office', country: 'GB', time_zone: 'Europe/London', mailboxes: ['m-leeds'], groups: [] }],
    workflows: [{ key: 'referrals', name: 'Referrals', mailboxes: [], labels: ['legal'] }],
    limits: [{ id: 'l-hf-uk', name: 'Never send UK faxes by HumbleFax', on: true,
      when: { destination: { countries: ['GB'] } }, then: { never: ['humblefax'] } }],
    routes: [{ id: 'r-uk', name: 'UK numbers go through Sinch', on: true, mandatory: true,
      when: { destination: { countries: ['GB'] } }, then: { try_in_order: ['sinch-uk', 'sip'] } }],
  };
}

interface ScopeState { name: string; revisions: RevisionDetail[]; draft: RulesState['draft'] }

export class FakeRules {
  requests: ApiRequest[] = [];
  scopes = new Map<string, ScopeState>();
  // Someone else saves the draft, or publishes, between a screen's read and its write.
  draftRace = false;
  publishRace = false;
  holds: Hold[] = [{
    id: 'h-1', job_id: 'job-1', kind: 'approval', to_number: '+15550100001', pages: 24, sender_name: 'Nia New',
    requested_at: '2026-10-07T16:00:00', until: null, reason: "Waiting for approval: the rule ‘Faxes over 20 pages need approval’ matched.",
    can_decide: true, version: 3,
  }, {
    id: 'h-2', job_id: 'job-2', kind: 'no_route', to_number: '+442071234567', pages: 1, sender_name: 'Ada Admin',
    requested_at: '2026-10-07T16:05:00', until: null, reason: 'No route your rules allow costs less than $0.10 for this fax.',
    can_decide: true, version: 1,
  }];
  accounts: AccountsState = {
    generation: 7, default_sending: 'sip', default_receiving: 'sip', sites: [{ key: 'leeds', name: 'Leeds office' }],
    providers: [
      { id: 'sinch', label: 'Sinch', supports_inbound: true, fields: [
        { name: 'project_id', label: 'Project ID', secret: false, required: true },
        { name: 'api_key', label: 'Access key', secret: true, required: true },
        { name: 'api_secret', label: 'Access secret', secret: true, required: true }] },
      { id: 'humblefax', label: 'HumbleFax', supports_inbound: false, fields: [] },
      { id: 'sip', label: 'Carrier trunk', supports_inbound: true, fields: [
        { name: 'host', label: 'Server', secret: false, required: true },
        { name: 'password', label: 'Password', secret: true, required: false }] },
    ],
    accounts: [
      { key: 'sip', provider: 'sip', label: 'Telnyx', site: null, primary: true, sends: true, receives: true, enabled: true,
        numbers: ['+17208565062'], limits: { at_once: 2, calls_per_second: 1, daily_limit: null },
        health: { state: 'ready', sentence: 'Ready to send and receive.' }, webhook_address: null,
        settings: { host: 'sip.telnyx.com' }, secrets_set: ['password'] },
      { key: 'sinch-uk', provider: 'sinch', label: 'Sinch (UK)', site: 'leeds', primary: false, sends: true, receives: true,
        enabled: true, numbers: ['+442071234567'], limits: { at_once: null, calls_per_second: null,
          daily_limit: { currency: 'USD', amount: '25' } },
        health: { state: 'waiting', sentence: 'Waiting for the first fax.' },
        webhook_address: 'https://fax.example/sinch-inbound/sinch-uk', settings: { project_id: 'proj-1' }, secrets_set: ['api_key'] },
    ],
  };

  constructor() {
    this.scopes.set('organization', { name: 'Organization', draft: null, revisions: [{
      number: 1, note: 'First rules', actor_name: 'Ada Admin', created_at: '2026-10-07T15:00:00', document: organizationDocument(),
    }] });
  }

  // Answers are copies, as from a real server, so a screen never shares objects with the fake.
  api() {
    return rulesApi(async <T,>(request: ApiRequest) => JSON.parse(JSON.stringify(await this.handle(request) ?? {})) as T);
  }

  sent(method: string, path: string): unknown[] {
    return this.requests.filter((request) => request.method === method && request.path.split('?')[0] === path)
      .map((request) => request.body);
  }

  scopeOf(path: string): [string, ScopeState] {
    const value = decodeURIComponent(new URL(path, 'https://x').searchParams.get('scope') ?? 'organization');
    if (!this.scopes.has(value)) this.scopes.set(value, { name: value.startsWith('mailbox:') ? 'Leeds intake' : 'Referrals', revisions: [], draft: null });
    return [value, this.scopes.get(value)!];
  }

  state(key: string, scope: ScopeState): RulesState {
    const organization = this.scopes.get('organization')!;
    return {
      scope: { kind: key === 'organization' ? 'organization' : key.startsWith('mailbox:') ? 'mailbox' : 'workflow',
        id: key.split(':')[1], name: scope.name },
      active: scope.revisions[scope.revisions.length - 1] ?? null,
      draft: scope.draft,
      organization: key === 'organization' ? null : organization.revisions[organization.revisions.length - 1] ?? null,
      matches_30_days: { 'r-uk': 12, 'l-hf-uk': 0 },
      can_write: true,
      choices: CHOICES,
    };
  }

  async handle(request: ApiRequest): Promise<unknown> {
    this.requests.push(request);
    const path = request.path.split('?')[0];
    const body = (request.body ?? {}) as Json;
    const fail = (status: number, detail: string) => { throw new AdminAPIError(status, 'Error', detail); };
    if (path.startsWith('/routing/rules') && path !== '/routing/rules/apply-to-waiting') {
      const [key, scope] = this.scopeOf(request.path);
      const active = scope.revisions[scope.revisions.length - 1] ?? null;
      if (path === '/routing/rules') return this.state(key, scope);
      if (path === '/routing/rules/draft' && request.method === 'PUT') {
        if (this.draftRace && scope.draft) { scope.draft.version += 1; this.draftRace = false; }
        const version = scope.draft?.version ?? 0;
        if (body.expected_version !== version) fail(409, 'Someone else changed these rules. Reload them and make your change again.');
        scope.draft = { document: body.document, version: version + 1, base_revision: active?.number ?? null, actor_name: 'Ada Admin',
          updated_at: '2026-10-07T16:00:00', check: { errors: [], warnings: [], replay: null } };
        return scope.draft;
      }
      if (path === '/routing/rules/draft' && request.method === 'DELETE') { scope.draft = null; return {}; }
      if (path === '/routing/rules/draft/check') {
        return { errors: [{ rule_id: null, message: 'The rule ‘Leeds first’ names a site that does not exist.' }],
          warnings: [{ rule_id: 'r-uk', message: 'The rule ‘UK numbers go through Sinch’ matched no fax in the last 30 days.' }],
          replay: { checked: 200, changed: 12, approximate: 3, items: [{ job_id: 'job-9', to_number: '+442071234567',
            accepted_at: '2026-10-06T10:00:00', before: 'Telnyx', after: 'Sinch (UK)', approximate: false }] } };
      }
      if (path === '/routing/rules/publish') {
        if (this.publishRace && active) {
          scope.revisions.push({ ...active, number: active.number + 1 });
          this.publishRace = false;
        }
        const head = scope.revisions[scope.revisions.length - 1] ?? null;
        if (!scope.draft || body.expected_active_revision !== (head?.number ?? null) || body.expected_draft_version !== scope.draft.version) {
          fail(409, 'Someone published other rules meanwhile. Reload them and check again.');
        }
        const revision = { number: (head?.number ?? 0) + 1, note: body.note, actor_name: 'Ada Admin', created_at: '2026-10-07T17:00:00',
          document: scope.draft!.document };
        scope.revisions.push(revision);
        scope.draft = null;
        return revision;
      }
      if (path === '/routing/rules/revisions') return { revisions: [...scope.revisions].reverse() };
      const parts = path.split('/');
      if (path.endsWith('/restore')) {
        const found = scope.revisions[Number(parts[4]) - 1];
        scope.draft = { document: found.document, version: (scope.draft?.version ?? 0) + 1, base_revision: found.number,
          actor_name: 'Ada Admin', updated_at: '2026-10-07T17:00:00', check: null };
        return scope.draft;
      }
      if (path.includes('/diff/')) {
        return { from: Number(parts[4]), to: Number(parts[6]), changes: [{ change: 'added', section: 'limits', id: 'l-big',
          name: 'Big faxes need approval', before: null, after: { when: { document: { pages_over: 20 } }, then: { hold_for_approval: {} } } }] };
      }
      return scope.revisions[Number(parts[4]) - 1];
    }
    if (path === '/routing/explain') {
      return { outcome: 'route', sentence: 'Sinch (UK) first, because the rule ‘UK numbers go through Sinch’ matched.',
        routes: [{ account: 'sinch-uk', label: 'Sinch (UK)', sentence: 'First in the rule.', quote: { currency: 'USD', amount: '0.031' },
          origin: 'Leeds office', usable: true },
        { account: 'humblefax', label: 'HumbleFax', sentence: 'Skipped: the limit ‘Never send UK faxes by HumbleFax’ applies.',
          quote: null, origin: null, usable: false }],
        holds: [], dial: null, page_layout: 'Pages per sheet: as the receiving machine allows.',
        trace: [{ scope: 'Organization', rule_id: 'r-uk', name: 'UK numbers go through Sinch', kind: 'route', matched: true, failed: null },
          { scope: 'Organization', rule_id: 'r-x', name: 'Clinics use the trunk', kind: 'route', matched: false,
            failed: 'The number is not in UK clinics.' }] };
    }
    if (path === '/routing/rules/apply-to-waiting') return { checked: 4, changed: 1, sentence: '1 of 4 waiting faxes will go differently.' };
    if (path === '/routing/holds') return { holds: this.holds };
    if (path.startsWith('/routing/holds/')) {
      const hold = this.holds.find((item) => item.id === path.split('/')[3])!;
      if (body.version !== hold.version) fail(409, 'Someone else decided on this fax meanwhile.');
      this.holds = this.holds.filter((item) => item.id !== hold.id);
      return { ...hold, version: hold.version + 1 };
    }
    if (path.startsWith('/routing/faxes/')) {
      return { job_id: path.split('/')[3], sentence: 'Sent by Sinch (UK) because the rule ‘UK numbers go through Sinch’ matched. Organization rules version 1.',
        attempts: [{ number: 1, account_label: 'Sinch (UK)', dialed_number: '+442071234567', page_layout: 'As the receiving machine allows',
          sentence: 'Delivered.', estimate: { currency: 'USD', amount: '0.031' } }], hold: null };
    }
    if (path === '/admin/providers/accounts' && request.method === 'GET') return this.accounts;
    if (path === '/admin/providers/accounts' && request.method === 'POST') {
      if (body.expected_generation !== this.accounts.generation) fail(409, 'Settings changed; reload before applying edits.');
      this.accounts.accounts.push({ key: body.key, provider: body.provider, label: body.label, site: body.site, primary: false,
        sends: body.sends, receives: body.receives, enabled: true, numbers: body.numbers, limits: body.limits,
        health: { state: 'waiting', sentence: 'Waiting for the first fax.' }, webhook_address: `https://fax.example/${body.provider}-inbound/${body.key}`,
        settings: body.settings, secrets_set: Object.keys(body.credentials) });
      this.accounts.generation += 1;
      return this.accounts;
    }
    if (path.startsWith('/admin/providers/accounts/') && request.method === 'PATCH') {
      const key = decodeURIComponent(path.split('/')[4]);
      const account = this.accounts.accounts.find((item) => item.key === key)!;
      const { expected_generation: _generation, default_sending: sending, default_receiving: receiving, credentials, ...rest } = body;
      Object.assign(account, rest);
      if (credentials) account.secrets_set = [...new Set([...account.secrets_set, ...Object.keys(credentials)])];
      if (sending) this.accounts.default_sending = key;
      if (receiving) this.accounts.default_receiving = key;
      this.accounts.generation += 1;
      return this.accounts;
    }
    throw new AdminAPIError(404, 'Not Found', 'Not Found');
  }
}
