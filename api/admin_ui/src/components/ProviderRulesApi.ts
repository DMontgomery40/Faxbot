// Provider rules and provider accounts: the shapes the console reads and writes, and one table of
// requests (method, address, body) for every rules, approvals and accounts route. The screens use
// `rulesApi(send)`; the console's API client supplies `send`, and tests supply an in-memory server.
// The rules document follows the provider-rules design (§4.1); the engine owns its meaning.
import type { Money } from '../api/deliveryTypes';

// -- the rules document --------------------------------------------------------------------------

export type Day = 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat' | 'sun';
export const DAYS: Day[] = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

export interface TimeCondition {
  days?: Day[];
  // Local time of day, 24-hour "HH:MM". A window may run past midnight (18:00 to 07:00).
  from?: string;
  until?: string;
  // Whose clock: the installation's (default) or the sender's site's.
  time_zone?: 'installation' | 'sender_site';
}

export interface DestinationCondition {
  numbers?: string[];
  lists?: string[];
  prefixes?: string[];
  countries?: string[];
  regions?: string[];
  // Saved recipients (Recipients), by id.
  recipients?: string[];
  partner?: boolean;
  own_number?: boolean;
  approved_alternate?: boolean;
  in_sender_country?: boolean;
}

export interface SenderCondition {
  people?: string[];
  keys?: string[];
  groups?: string[];
  mailboxes?: string[];
  sites?: string[];
}

export interface DocumentCondition {
  pages_over?: number;
  pages_under?: number;
  size_over?: number;
  case_packet?: boolean;
}

export interface Conditions {
  destination?: DestinationCondition;
  sender?: SenderCondition;
  workflows?: string[];
  document?: DocumentCondition;
  urgent?: boolean;
  real_call?: boolean;
  labels?: string[];
  time?: TimeCondition;
}

export type RouteMode = 'ordered' | 'cheapest_reliable';
export type PageLayout = 'as_receiver_allows' | 'one_per_sheet';
export type AlternateNumber = 'use' | 'never' | 'only';

export interface Actions {
  // Routing actions (one per routing rule).
  use?: string;
  try_in_order?: string[];
  cheapest_reliable?: string[];
  site_accounts?: string;  // 'sender' or a site key
  mode?: RouteMode;
  automatic?: boolean;
  // Route settings.
  when_busy?: 'wait' | 'next';
  page_layout?: PageLayout;
  alternate_number?: AlternateNumber;
  // Limits.
  never?: string[];
  require_direct?: boolean;
  require_encryption?: boolean;
  cap_cost?: Money;
  hold_for_approval?: { separate_approver?: boolean };
  hold_until?: TimeCondition;
  place_a_real_call?: boolean;
}

export interface Rule {
  id: string;
  name: string;
  on: boolean;
  mandatory?: boolean;
  when: Conditions;
  unless?: Conditions;
  then: Actions;
}

export interface RecipientList { name: string; numbers?: string[]; prefixes?: string[] }
export interface Region { name: string; countries?: string[]; prefixes?: string[] }
export interface Site {
  key: string; name: string; country?: string; time_zone?: string; mailboxes?: string[]; groups?: string[];
  // Accounts the site lists; accounts whose own site names it belong to it too.
  accounts?: string[];
}
export interface Workflow { key: string; name: string; mailboxes?: string[]; labels?: string[] }

export interface RulesDocument {
  format: 1;
  // Named recipient groups.
  lists?: Record<string, RecipientList>;
  // The labels senders may attach to a fax.
  labels?: string[];
  regions?: Record<string, Region>;
  sites?: Site[];
  workflows?: Workflow[];
  limits?: Rule[];
  routes?: Rule[];
}

export type RuleKind = 'limits' | 'routes';

export function emptyDocument(): RulesDocument {
  return { format: 1, limits: [], routes: [] };
}

export function recipientLists(document: RulesDocument): Record<string, RecipientList> {
  return document.lists ?? {};
}

export function documentLabels(document: RulesDocument): string[] {
  return document.labels ?? [];
}

// A copy of the document with its recipient groups and labels replaced.
export function withLists(document: RulesDocument, lists: Record<string, RecipientList>, labels: string[]): RulesDocument {
  return { ...document, lists, labels };
}

// -- scopes ---------------------------------------------------------------------------------------

export type ScopeKind = 'organization' | 'mailbox' | 'workflow';
export interface Scope { kind: ScopeKind; id?: string }
export const ORGANIZATION: Scope = { kind: 'organization' };

// The scope as the API's `scope` query value: organization, mailbox:ID or workflow:KEY.
export function scopeParam(scope: Scope): string {
  return scope.kind === 'organization' ? 'organization' : `${scope.kind}:${scope.id ?? ''}`;
}

// -- what the rules routes return ------------------------------------------------------------------

export interface Revision {
  number: number;
  note: string | null;
  actor_name: string | null;
  created_at: string;
}

export interface RevisionDetail extends Revision { document: RulesDocument }

export interface Issue {
  // The rule, list, site or other entry the issue is about, when there is one.
  rule_id?: string | null;
  message: string;
}

export interface ReplayItem {
  job_id: string;
  to_number: string;
  accepted_at: string;
  before: string;
  after: string;
  // Accepted before rules existed: replayed with today's groups and preferences.
  approximate: boolean;
}

export interface CheckResult {
  errors: Issue[];
  warnings: Issue[];
  replay: { checked: number; changed: number; approximate: number; items: ReplayItem[] } | null;
}

export interface Draft {
  document: RulesDocument;
  version: number;
  base_revision: number | null;
  actor_name: string | null;
  updated_at: string;
  check: CheckResult | null;
}

export interface AccountChoice {
  key: string; label: string; provider: string; sends: boolean; enabled: boolean; site?: string | null;
}

// Names the editor offers and the sentences use.
export interface Choices {
  accounts: AccountChoice[];
  people: Array<{ id: string; name: string; kind: 'user' | 'integration' }>;
  keys: Array<{ id: string; name: string }>;
  groups: Array<{ id: string; name: string }>;
  mailboxes: Array<{ id: string; name: string }>;
  // Saved recipients (Recipients), for the "saved recipient" condition.
  recipients?: Array<{ id: string; name: string }>;
}

export interface RulesState {
  scope: Scope & { name: string };
  active: RevisionDetail | null;
  draft: Draft | null;
  // The organization's rules above a mailbox or workflow scope, shown read-only.
  organization: RevisionDetail | null;
  // Faxes each rule matched in the last 30 days, by rule id.
  matches_30_days: Record<string, number>;
  can_write: boolean;
  choices: Choices;
  // The installation's time zone, which rules about times of day and "Try a fax" use.
  time_zone: string;
}

export interface DiffChange {
  change: 'added' | 'removed' | 'changed' | 'moved';
  section: 'limits' | 'routes' | 'lists' | 'labels' | 'regions' | 'sites' | 'workflows';
  id: string;
  name: string;
  before: unknown;
  after: unknown;
}

export interface Diff { from: number; to: number; changes: DiffChange[] }

// -- the dry run -----------------------------------------------------------------------------------

export interface ExplainRequest {
  to: string;
  pages?: number | null;
  size_bytes?: number | null;
  // A person or integration id, or 'me'.
  as?: string | null;
  mailbox?: string | null;
  workflow?: string | null;
  urgent?: boolean;
  real_call?: boolean;
  labels?: string[];
  // A local time to try ("2026-10-07T18:30"), or now.
  at?: string | null;
  // The rules to try: the active ones, the draft, or an earlier revision of the scope.
  source: 'active' | 'draft' | { revision: number };
  scope?: string;
}

export type StepResult = 'matched' | 'not_matched' | 'unless' | 'not_reached' | 'not_applied';

// One line of the trace (the engine's Step): a rule, what happened to it, and the first field that decided it.
export interface TraceStep {
  kind: 'limit' | 'route' | 'preferred';
  result: StepResult;
  // 'organization', 'mailbox:ID' or 'workflow:KEY'.
  scope: string;
  // The rule's name, or for the preferred route "Your preferred route for this number: …".
  name?: string | null;
  // The first condition that did not match, as a sentence.
  failed?: string | null;
  // The mailbox's or workflow's name, for those scopes.
  scope_name?: string | null;
  revision?: number | null;
  rule_id?: string | null;
  rule_name?: string | null;
  // The first condition that did not match, such as 'destination.countries'.
  field?: string | null;
  // Why a matching rule did not decide: 'mandatory', 'excluded', 'overridden'.
  note?: string | null;
}

export interface ExplainRoute {
  account: string;
  label: string;
  sentence: string;
  quote: Money | null;
  origin: string | null;
  usable: boolean;
}

export interface ExplainResult {
  outcome: 'route' | 'held' | 'blocked';
  sentence: string;
  routes: ExplainRoute[];
  holds: string[];
  dial: { number: string; sentence: string } | null;
  page_layout: string | null;
  trace: TraceStep[];
}

// -- held faxes and provenance ---------------------------------------------------------------------

export interface Hold {
  id: string;
  job_id: string;
  kind: 'approval' | 'window' | 'no_route';
  to_number: string;
  pages: number | null;
  sender_name: string | null;
  requested_at: string;
  until: string | null;
  reason: string;
  // False when the rule needs someone other than the sender and this person sent it.
  can_decide: boolean;
  version: number;
  // A fax with no route the rules allow: the accounts it may still be sent by anyway, each left out only by a
  // cost cap or by being down or busy, with the reason ("over the $0.50 cap: estimated $0.90").
  options?: SendAnywayOption[];
  // Why other accounts are not offered (a mandatory limit, a never rule, direct delivery required).
  not_offered?: string[];
}

export interface SendAnywayOption { account: string; label: string; reason: string }

// The recipient's approved alternate number an attempt dialed instead of the number the sender gave.
export interface AlternateDial {
  original_number: string;
  approved_by: string | null;
  // The local calendar date of the approval ("2026-10-07").
  approved_on: string | null;
  note: string | null;
  // A toll-free number: the recipient pays for the call.
  recipient_pays: boolean;
}

export interface RouteAttempt {
  number: number;
  account_label: string;
  dialed_number: string | null;
  alternate?: AlternateDial | null;
  page_layout: string | null;
  sentence: string;
  estimate: Money | null;
}

// What approving or refusing changed, with the server's own sentence for what happens next.
export type HoldDecision = Hold & { sentence?: string | null };

export interface FaxRoute {
  job_id: string;
  sentence: string;
  attempts: RouteAttempt[];
  hold: Hold | null;
  // Every rule, from replaying the fax's stored facts under the rules it was accepted with.
  trace?: TraceStep[];
}

// -- provider accounts -----------------------------------------------------------------------------

export type HealthState = 'ready' | 'not_set_up' | 'off' | 'waiting' | 'failing' | 'spending_limit';

export interface AccountLimits { at_once: number | null; calls_per_second: number | null; daily_limit: Money | null }

export interface ProviderAccount {
  key: string;
  provider: string;
  label: string;
  site: string | null;
  primary: boolean;
  sends: boolean;
  receives: boolean;
  enabled: boolean;
  numbers: string[];
  limits: AccountLimits;
  health: { state: HealthState; sentence: string };
  // The address to give the provider for received faxes, when this account receives by notification.
  webhook_address: string | null;
  settings: Record<string, string | number | boolean | null>;
  // Secret fields that hold a value; secrets themselves are never sent back.
  secrets_set: string[];
}

export interface ProviderField {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  help?: string | null;
}

export interface ProviderKind {
  id: string;
  label: string;
  supports_inbound: boolean;
  fields: ProviderField[];
}

export interface AccountsState {
  generation: number;
  default_sending: string | null;
  default_receiving: string | null;
  accounts: ProviderAccount[];
  providers: ProviderKind[];
  sites: Array<{ key: string; name: string }>;
}

export interface AccountInput {
  key: string;
  provider: string;
  label: string;
  site: string | null;
  sends: boolean;
  receives: boolean;
  numbers: string[];
  limits: AccountLimits;
  settings: Record<string, string | number | boolean | null>;
  credentials: Record<string, string>;
}

export type AccountPatch = Partial<Omit<AccountInput, 'key' | 'provider'>> & {
  enabled?: boolean; default_sending?: boolean; default_receiving?: boolean;
};

export interface AccountHealth {
  key: string;
  state: HealthState;
  sentence: string;
  details: string[];
}

// -- receiving rules (Numbers) ---------------------------------------------------------------------

// What a number rule adds to "faxes to this number go to this mailbox" (design §4.9). A rule without
// options behaves exactly as number rules always have.
export interface ReceivingOptions {
  // The rule's place among number rules; the first that matches a received fax places it.
  position: number | null;
  enabled: boolean;
  // Matches a fax to any of your numbers, not only this rule's number.
  any_number: boolean;
  // Only faxes that arrive on this account.
  account_key: string | null;
  // Only faxes from these numbers; an entry ending in * matches numbers that start with it.
  from_numbers: string[];
  days: Day[];
  // Minutes after midnight, in the installation's time zone; a window may run past midnight.
  start_minute: number | null;
  end_minute: number | null;
  // Deliver by email through this connector instead of the usual one; email_off sends no email at all.
  email_connector_id: string | null;
  email_off: boolean;
  urgent: boolean;
  // How long the received fax is kept before cleanup removes it. Not a legal hold.
  keep_days: number | null;
}

export const NO_RECEIVING_OPTIONS: ReceivingOptions = {
  position: null, enabled: true, any_number: false, account_key: null, from_numbers: [], days: [], start_minute: null,
  end_minute: null, email_connector_id: null, email_off: false, urgent: false, keep_days: null,
};

export interface ReceivedExplainRequest {
  to_number: string;
  from_number: string | null;
  account_key: string | null;
  // A local time to try ("2026-10-07T18:30"), or now.
  at: string | null;
}

export interface ReceivedExplainResult {
  sentence: string;
  mailbox_label: string | null;
  email: string | null;
  urgent: boolean;
  keep_days: number | null;
  // The number rule that placed it, or null when the usual placement applies.
  rule_to_number: string | null;
}

// -- requests ---------------------------------------------------------------------------------------

export type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
export interface ApiRequest { method: Method; path: string; body?: unknown }
export type Send = <T>(request: ApiRequest) => Promise<T>;

const segment = (value: string | number) => encodeURIComponent(String(value));
const scoped = (path: string, scope: Scope) => `${path}?scope=${segment(scopeParam(scope))}`;

// Every request the rules, approvals and accounts screens make.
export const requests = {
  rules: (scope: Scope): ApiRequest => ({ method: 'GET', path: scoped('/routing/rules', scope) }),
  saveDraft: (scope: Scope, document: RulesDocument, expectedVersion: number): ApiRequest => ({
    method: 'PUT', path: scoped('/routing/rules/draft', scope), body: { document, expected_version: expectedVersion },
  }),
  discardDraft: (scope: Scope): ApiRequest => ({ method: 'DELETE', path: scoped('/routing/rules/draft', scope) }),
  checkDraft: (scope: Scope, replay = 200): ApiRequest => ({
    method: 'POST', path: scoped('/routing/rules/draft/check', scope), body: { replay },
  }),
  publish: (scope: Scope, expectedActive: number | null, expectedDraft: number, note: string): ApiRequest => ({
    method: 'POST', path: scoped('/routing/rules/publish', scope),
    body: { expected_active_revision: expectedActive, expected_draft_version: expectedDraft, note },
  }),
  revisions: (scope: Scope): ApiRequest => ({ method: 'GET', path: scoped('/routing/rules/revisions', scope) }),
  revision: (scope: Scope, number: number): ApiRequest => ({
    method: 'GET', path: scoped(`/routing/rules/revisions/${segment(number)}`, scope),
  }),
  diff: (scope: Scope, from: number, to: number): ApiRequest => ({
    method: 'GET', path: scoped(`/routing/rules/revisions/${segment(from)}/diff/${segment(to)}`, scope),
  }),
  restore: (scope: Scope, number: number): ApiRequest => ({
    method: 'POST', path: scoped(`/routing/rules/revisions/${segment(number)}/restore`, scope), body: {},
  }),
  explain: (body: ExplainRequest): ApiRequest => ({ method: 'POST', path: '/routing/explain', body }),
  applyToWaiting: (): ApiRequest => ({ method: 'POST', path: '/routing/rules/apply-to-waiting', body: {} }),
  faxRoute: (jobId: string): ApiRequest => ({ method: 'GET', path: `/routing/faxes/${segment(jobId)}/route` }),
  holds: (): ApiRequest => ({ method: 'GET', path: '/routing/holds?state=open' }),
  // For a fax with no route the rules allow, `account` is the one to send it by anyway.
  approve: (hold: Hold, account?: string): ApiRequest => ({
    method: 'POST', path: `/routing/holds/${segment(hold.id)}/approve`,
    body: account ? { version: hold.version, account } : { version: hold.version },
  }),
  checkAgain: (hold: Hold): ApiRequest => ({
    method: 'POST', path: `/routing/holds/${segment(hold.id)}/check-again`, body: { version: hold.version },
  }),
  refuse: (hold: Hold, reason: string): ApiRequest => ({
    method: 'POST', path: `/routing/holds/${segment(hold.id)}/refuse`, body: { version: hold.version, reason },
  }),
  accounts: (): ApiRequest => ({ method: 'GET', path: '/admin/providers/accounts' }),
  addAccount: (input: AccountInput, generation: number): ApiRequest => ({
    method: 'POST', path: '/admin/providers/accounts', body: { ...input, expected_generation: generation },
  }),
  updateAccount: (key: string, patch: AccountPatch, generation: number): ApiRequest => ({
    method: 'PATCH', path: `/admin/providers/accounts/${segment(key)}`, body: { ...patch, expected_generation: generation },
  }),
  explainReceived: (body: ReceivedExplainRequest): ApiRequest => ({
    method: 'POST', path: '/access/inbound-rules/explain', body,
  }),
  accountHealth: (key: string): ApiRequest => ({
    method: 'GET', path: `/admin/providers/accounts/${segment(key)}/health`,
  }),
};

export interface RulesApi {
  rules(scope: Scope): Promise<RulesState>;
  saveDraft(scope: Scope, document: RulesDocument, expectedVersion: number): Promise<Draft>;
  discardDraft(scope: Scope): Promise<void>;
  checkDraft(scope: Scope, replay?: number): Promise<CheckResult>;
  publish(scope: Scope, expectedActive: number | null, expectedDraft: number, note: string): Promise<Revision>;
  revisions(scope: Scope): Promise<{ revisions: Revision[] }>;
  revision(scope: Scope, number: number): Promise<RevisionDetail>;
  diff(scope: Scope, from: number, to: number): Promise<Diff>;
  restore(scope: Scope, number: number): Promise<Draft>;
  explain(body: ExplainRequest): Promise<ExplainResult>;
  applyToWaiting(): Promise<{ changed: number; checked: number; sentence: string }>;
  faxRoute(jobId: string): Promise<FaxRoute>;
  holds(): Promise<{ holds: Hold[] }>;
  approve(hold: Hold, account?: string): Promise<HoldDecision>;
  checkAgain(hold: Hold): Promise<HoldDecision>;
  refuse(hold: Hold, reason: string): Promise<HoldDecision>;
  accounts(): Promise<AccountsState>;
  addAccount(input: AccountInput, generation: number): Promise<AccountsState>;
  updateAccount(key: string, patch: AccountPatch, generation: number): Promise<AccountsState>;
  accountHealth(key: string): Promise<AccountHealth>;
  explainReceived(body: ReceivedExplainRequest): Promise<ReceivedExplainResult>;
}

// The rules API of a console API client, one per client, so screens see the same object on every render.
const perClient = new WeakMap<object, RulesApi>();
export function rulesApiFor(client: { call: <T>(request: ApiRequest) => Promise<T> }): RulesApi {
  let api = perClient.get(client);
  if (!api) {
    api = rulesApi(<T,>(request: ApiRequest) => client.call<T>(request));
    perClient.set(client, api);
  }
  return api;
}

// The rules API over one request function.
export function rulesApi(send: Send): RulesApi {
  return {
    rules: (scope) => send(requests.rules(scope)),
    saveDraft: (scope, document, expectedVersion) => send(requests.saveDraft(scope, document, expectedVersion)),
    discardDraft: async (scope) => { await send(requests.discardDraft(scope)); },
    checkDraft: (scope, replay) => send(requests.checkDraft(scope, replay)),
    publish: (scope, expectedActive, expectedDraft, note) => send(requests.publish(scope, expectedActive, expectedDraft, note)),
    revisions: (scope) => send(requests.revisions(scope)),
    revision: (scope, number) => send(requests.revision(scope, number)),
    diff: (scope, from, to) => send(requests.diff(scope, from, to)),
    restore: (scope, number) => send(requests.restore(scope, number)),
    explain: (body) => send(requests.explain(body)),
    applyToWaiting: () => send(requests.applyToWaiting()),
    faxRoute: (jobId) => send(requests.faxRoute(jobId)),
    holds: () => send(requests.holds()),
    approve: (hold, account) => send(requests.approve(hold, account)),
    checkAgain: (hold) => send(requests.checkAgain(hold)),
    refuse: (hold, reason) => send(requests.refuse(hold, reason)),
    accounts: () => send(requests.accounts()),
    addAccount: (input, generation) => send(requests.addAccount(input, generation)),
    updateAccount: (key, patch, generation) => send(requests.updateAccount(key, patch, generation)),
    accountHealth: (key) => send(requests.accountHealth(key)),
    explainReceived: (body) => send(requests.explainReceived(body)),
  };
}
