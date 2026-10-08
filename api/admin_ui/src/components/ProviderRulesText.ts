// Provider rules in words: each rule reads as one sentence ("When the destination country is United
// Kingdom, try Sinch (UK), then Telnyx."), followed by any route settings in their own sentences.
// The faxbot command builds the same sentences (api/app/cli/commands/rules.py); a shared fixture
// (__tests__/providerRulesSentences.json) keeps the two identical.
import type { Money } from '../api/deliveryTypes';
import { countryName } from './common/numbers';
import { formatMoney } from './delivery/shared';
import type {
  Actions, Choices, Conditions, Day, ReceivingOptions, RecipientList, Region, Rule, RulesDocument, TimeCondition,
} from './ProviderRulesApi';
import { DAYS, recipientLists } from './ProviderRulesApi';

export interface Names {
  account: (key: string) => string;
  list: (key: string) => string;
  region: (key: string) => string;
  site: (key: string) => string;
  workflow: (key: string) => string;
  person: (id: string) => string;
  recipient: (id: string) => string;
  key: (id: string) => string;
  group: (id: string) => string;
  mailbox: (id: string) => string;
  country: (code: string) => string;
  money: (value: Money) => string;
}

// Plain maps of names, as the shared fixture and the API's choices give them.
export interface NameMaps {
  accounts?: Record<string, string>;
  lists?: Record<string, string>;
  regions?: Record<string, string>;
  sites?: Record<string, string>;
  workflows?: Record<string, string>;
  people?: Record<string, string>;
  recipients?: Record<string, string>;
  keys?: Record<string, string>;
  groups?: Record<string, string>;
  mailboxes?: Record<string, string>;
  countries?: Record<string, string>;
}

const BUILT_IN_ROUTES: Record<string, string> = { direct: 'direct delivery', local: 'delivery inside Faxbot' };

export function namesFrom(maps: NameMaps): Names {
  const pick = (map: Record<string, string> | undefined, fallback: (value: string) => string) =>
    (value: string) => map?.[value] ?? fallback(value);
  return {
    account: pick(maps.accounts, (key) => BUILT_IN_ROUTES[key] ?? key),
    list: pick(maps.lists, (key) => key),
    region: pick(maps.regions, (key) => key),
    site: pick(maps.sites, (key) => key),
    workflow: pick(maps.workflows, (key) => key),
    person: pick(maps.people, () => 'an unknown person'),
    recipient: pick(maps.recipients, () => 'an unknown recipient'),
    key: pick(maps.keys, () => 'an unknown key'),
    group: pick(maps.groups, () => 'an unknown group'),
    mailbox: pick(maps.mailboxes, () => 'an unknown mailbox'),
    country: pick(maps.countries, countryName),
    money: formatMoney,
  };
}

// The names a rules page knows: its accounts and the document's own lists, regions, sites and workflows.
export function namesFor(document: RulesDocument | null, choices: Choices | null): Names {
  const byKey = <T>(entries: Array<[string, T]>, name: (value: T) => string) =>
    Object.fromEntries(entries.map(([key, value]) => [key, name(value)]));
  const lists = recipientLists(document ?? { format: 1 });
  return namesFrom({
    accounts: Object.fromEntries((choices?.accounts ?? []).map((account) => [account.key, account.label])),
    lists: byKey(Object.entries(lists), (list: RecipientList) => list.name),
    regions: byKey(Object.entries(document?.regions ?? {}), (region: Region) => region.name),
    sites: Object.fromEntries((document?.sites ?? []).map((site) => [site.key, site.name])),
    workflows: Object.fromEntries((document?.workflows ?? []).map((workflow) => [workflow.key, workflow.name])),
    people: Object.fromEntries((choices?.people ?? []).map((person) => [person.id, person.name])),
    recipients: Object.fromEntries((choices?.recipients ?? []).map((recipient) => [recipient.id, recipient.name])),
    keys: Object.fromEntries((choices?.keys ?? []).map((key) => [key.id, key.name])),
    groups: Object.fromEntries((choices?.groups ?? []).map((group) => [group.id, group.name])),
    mailboxes: Object.fromEntries((choices?.mailboxes ?? []).map((mailbox) => [mailbox.id, mailbox.name])),
  });
}

// "A", "A and B", "A, B and C".
function joined(items: string[], word: 'and' | 'or'): string {
  if (items.length <= 1) return items[0] ?? '';
  return `${items.slice(0, -1).join(', ')} ${word} ${items[items.length - 1]}`;
}

export const joinAnd = (items: string[]) => joined(items, 'and');
export const joinOr = (items: string[]) => joined(items, 'or');

const DAY_NAMES: Record<Day, string> = {
  mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday',
};

export function daysText(days: Day[] | undefined): string | null {
  if (!days || days.length === 0) return null;
  const set = new Set(days);
  const ordered = DAYS.filter((day) => set.has(day));
  if (ordered.length === 7) return 'every day';
  if (ordered.join(',') === 'mon,tue,wed,thu,fri') return 'on weekdays';
  if (ordered.join(',') === 'sat,sun') return 'at weekends';
  return `on ${joinAnd(ordered.map((day) => DAY_NAMES[day]))}`;
}

// "on weekdays between 18:00 and 07:00 (the sender's site's time)".
export function windowText(time: TimeCondition | undefined): string | null {
  if (!time) return null;
  const parts: string[] = [];
  const days = daysText(time.days);
  if (days) parts.push(days);
  if (time.from && time.until) parts.push(`between ${time.from} and ${time.until}`);
  else if (time.from) parts.push(`after ${time.from}`);
  else if (time.until) parts.push(`before ${time.until}`);
  if (parts.length === 0) return null;
  const zone = time.time_zone === 'sender_site' ? " (the sender's site's time)" : '';
  return `${parts.join(' ')}${zone}`;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function megabytes(bytes: number): string {
  const mb = bytes / 1_000_000;
  return Number.isInteger(mb) ? String(mb) : String(Math.round(mb * 10) / 10);
}

function flag(value: boolean | undefined, yes: string, no: string): string[] {
  if (value === undefined || value === null) return [];
  return [value ? yes : no];
}

// Each condition of a `when` or `unless` block as a clause, in a fixed order.
export function conditionClauses(conditions: Conditions | undefined, names: Names): string[] {
  if (!conditions) return [];
  const clauses: string[] = [];
  const destination = conditions.destination ?? {};
  const numbers = destination.numbers ?? [];
  if (numbers.length > 3) clauses.push(`the number is one of ${numbers.length} numbers`);
  else if (numbers.length > 0) clauses.push(`the number is ${joinOr(numbers)}`);
  if (destination.lists?.length) clauses.push(`the number is in ${joinOr(destination.lists.map(names.list))}`);
  if (destination.prefixes?.length) clauses.push(`the number starts with ${joinOr(destination.prefixes)}`);
  if (destination.countries?.length) {
    clauses.push(`the destination country is ${joinOr(destination.countries.map(names.country))}`);
  }
  if (destination.regions?.length) clauses.push(`the number is in ${joinOr(destination.regions.map(names.region))}`);
  if (destination.recipients?.length) clauses.push(`the recipient is ${joinOr(destination.recipients.map(names.recipient))}`);
  clauses.push(...flag(destination.partner, 'the number has a verified partner', 'the number has no verified partner'));
  clauses.push(...flag(destination.own_number, 'the number is one of your own numbers',
    'the number is not one of your own numbers'));
  clauses.push(...flag(destination.approved_alternate, 'the recipient has an approved alternate number',
    'the recipient has no approved alternate number'));
  clauses.push(...flag(destination.in_sender_country, "the destination is in the sender's site's country",
    "the destination is outside the sender's site's country"));
  const sender = conditions.sender ?? {};
  if (sender.people?.length) clauses.push(`the sender is ${joinOr(sender.people.map(names.person))}`);
  if (sender.keys?.length) clauses.push(`the fax comes from the key ${joinOr(sender.keys.map(names.key))}`);
  if (sender.groups?.length) clauses.push(`the sender is in ${joinOr(sender.groups.map(names.group))}`);
  if (sender.mailboxes?.length) {
    clauses.push(`the fax is sent from the mailbox ${joinOr(sender.mailboxes.map(names.mailbox))}`);
  }
  if (sender.sites?.length) clauses.push(`the fax is sent from ${joinOr(sender.sites.map(names.site))}`);
  if (conditions.workflows?.length) clauses.push(`the fax is part of ${joinOr(conditions.workflows.map(names.workflow))}`);
  const document = conditions.document ?? {};
  if (document.pages_over !== undefined && document.pages_over !== null) {
    clauses.push(`the fax has more than ${plural(document.pages_over, 'page', 'pages')}`);
  }
  if (document.pages_under !== undefined && document.pages_under !== null) {
    clauses.push(`the fax has fewer than ${plural(document.pages_under, 'page', 'pages')}`);
  }
  if (document.size_over !== undefined && document.size_over !== null) {
    clauses.push(`the file is larger than ${megabytes(document.size_over)} MB`);
  }
  clauses.push(...flag(document.case_packet, 'the fax is a case packet', 'the fax is not a case packet'));
  clauses.push(...flag(conditions.urgent, 'the fax is marked urgent', 'the fax is not marked urgent'));
  clauses.push(...flag(conditions.real_call, 'the sender asked for a real call', 'the sender did not ask for a real call'));
  if (conditions.labels?.length) clauses.push(`the fax is labelled ${joinOr(conditions.labels)}`);
  const window = windowText(conditions.time);
  if (window) clauses.push(`the fax is sent ${window}`);
  return clauses;
}

export function accountsInOrder(keys: string[], names: Names): string {
  return keys.map(names.account).join(', then ');
}

// What a rule does, as phrases joined into the sentence ("try Sinch (UK), then Telnyx").
export function actionPhrases(then: Actions, names: Names): string[] {
  const phrases: string[] = [];
  if (then.use) phrases.push(`use ${names.account(then.use)}`);
  if (then.try_in_order?.length) {
    phrases.push(then.try_in_order.length === 1 ? `use ${names.account(then.try_in_order[0])}`
      : `try ${accountsInOrder(then.try_in_order, names)}`);
  }
  if (then.cheapest_reliable?.length) {
    phrases.push(then.cheapest_reliable.length === 1 ? `use ${names.account(then.cheapest_reliable[0])}`
      : `use the cheapest reliable of ${joinAnd(then.cheapest_reliable.map(names.account))}`);
  }
  if (then.site_accounts) {
    const whose = then.site_accounts === 'sender' ? "the sender's site's" : `${names.site(then.site_accounts)}'s`;
    const order = then.mode === 'ordered' ? 'in order' : 'cheapest reliable first';
    phrases.push(`use ${whose} accounts, ${order}`);
  }
  if (then.automatic) phrases.push('choose the cheapest reliable route, as before');
  if (then.never?.length) phrases.push(`never use ${joinOr(then.never.map(names.account))}`);
  if (then.require_direct) phrases.push('send only by direct delivery to a verified partner');
  if (then.require_encryption) {
    phrases.push('send only encrypted (direct delivery, or SSL Fax where this number has used it before)');
  }
  if (then.cap_cost) phrases.push(`use only routes that cost at most ${names.money(then.cap_cost)} for the fax`);
  if (then.hold_for_approval) {
    phrases.push(then.hold_for_approval.separate_approver
      ? 'hold the fax for approval by someone other than the sender' : 'hold the fax for approval');
  }
  const window = windowText(then.hold_until);
  if (window) phrases.push(`send the fax only ${window}`);
  if (then.place_a_real_call) phrases.push('place a real call, even to your own numbers');
  return phrases;
}

// The route settings, each its own sentence.
export function settingSentences(then: Actions): string[] {
  const sentences: string[] = [];
  if (then.when_busy === 'next') sentences.push('If every line is busy, Faxbot uses the next account.');
  if (then.when_busy === 'wait') sentences.push('If every line is busy, Faxbot waits for a free line.');
  if (then.page_layout === 'as_receiver_allows') sentences.push('Pages per sheet: as the receiving machine allows.');
  if (then.page_layout === 'one_per_sheet') sentences.push('Pages per sheet: one.');
  if (then.alternate_number === 'use') {
    sentences.push("Faxbot dials the recipient's approved alternate number when there is one.");
  }
  if (then.alternate_number === 'only') {
    sentences.push("Faxbot dials only the recipient's approved alternate number, and holds the fax when there is none.");
  }
  if (then.alternate_number === 'never') sentences.push('Faxbot always dials the number the sender gave.');
  return sentences;
}

// The whole rule in words. Conditions are joined by "and" throughout, because a condition can
// hold its own list ("United Kingdom or Ireland") and commas would blur where each one ends.
export function ruleSentence(rule: Pick<Rule, 'when' | 'unless' | 'then'>, names: Names): string {
  const when = conditionClauses(rule.when, names);
  const unless = conditionClauses(rule.unless, names);
  let opening = when.length > 0 ? `When ${when.join(' and ')}` : 'For every fax';
  if (unless.length > 0) opening += `, unless ${unless.join(' and ')}`;
  const actions = actionPhrases(rule.then, names);
  const main = `${opening}, ${actions.length > 0 ? joinAnd(actions) : 'change nothing'}.`;
  return [main, ...settingSentences(rule.then)].join(' ');
}

// Minutes after midnight as a 24-hour time ("18:00").
export function minutesText(minutes: number): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(Math.floor(minutes / 60) % 24)}:${pad(minutes % 60)}`;
}

export function textMinutes(time: string): number | null {
  const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(time);
  return match ? Number(match[1]) * 60 + Number(match[2]) : null;
}

// A number rule in words: "Faxes to +17208565062 received on Telnyx from numbers starting with +1303 on weekdays
// between 09:00 and 17:00 go to Front desk, marked urgent, with no email, kept for 30 days."
export function receivingSentence(rule: { to_number: string; mailbox_label: string } & Partial<ReceivingOptions>,
  names: { account: (key: string) => string; connector: (id: string) => string; site?: (key: string) => string }): string {
  let sentence = rule.any_number || !rule.to_number ? 'Faxes to any of your numbers' : `Faxes to ${rule.to_number}`;
  if (rule.subaddress) sentence += ` with subaddress ${rule.subaddress}`;
  if (rule.account_key) sentence += ` received on ${names.account(rule.account_key)}`;
  else if (rule.site_key) sentence += ` received on an account of ${names.site ? names.site(rule.site_key) : rule.site_key}`;
  const from = (rule.from_numbers ?? []).map((entry) => (entry.endsWith('*')
    ? `numbers starting with ${entry.slice(0, -1)}` : entry));
  if (from.length > 0) sentence += ` from ${joinOr(from)}`;
  const window = windowText({
    days: rule.days ?? [],
    from: rule.start_minute === null || rule.start_minute === undefined ? undefined : minutesText(rule.start_minute),
    until: rule.end_minute === null || rule.end_minute === undefined ? undefined : minutesText(rule.end_minute),
  });
  if (window) sentence += ` ${window}`;
  sentence += ` go to ${rule.mailbox_label}`;
  if (rule.urgent) sentence += ', marked urgent';
  if (rule.email_off) sentence += ', with no email';
  else if (rule.email_connector_id) sentence += `, emailed through ${names.connector(rule.email_connector_id)}`;
  if (rule.keep_days) sentence += `, kept for ${rule.keep_days === 1 ? '1 day' : `${rule.keep_days} days`}`;
  return `${sentence}.`;
}

export const KEEP_DAYS_NOTE = 'This is when cleanup removes the fax from Faxbot. It is not a legal hold, and it does not '
  + 'promise to keep the fax that long.';

// The installation's currency, from its country, as the faxbot command works it out (cli/output.py).
const COUNTRY_CURRENCY: Record<string, string> = {
  US: 'USD', PR: 'USD', CA: 'CAD', GB: 'GBP', AU: 'AUD', NZ: 'NZD', SG: 'SGD', HK: 'HKD', JP: 'JPY', IN: 'INR', CH: 'CHF',
  IE: 'EUR', DE: 'EUR', FR: 'EUR', ES: 'EUR', IT: 'EUR', NL: 'EUR', BE: 'EUR', AT: 'EUR', PT: 'EUR', FI: 'EUR', GR: 'EUR',
  LU: 'EUR',
};

export function currencyFor(country: string | null | undefined): string {
  return COUNTRY_CURRENCY[country || 'US'] ?? 'USD';
}

// A page layout as the engine names it, in words: "as the receiving machine allows" or "one".
export function layoutWords(layout: string): string {
  if (layout === 'as_receiver_allows') return 'as the receiving machine allows';
  if (layout === 'one_per_sheet') return 'one';
  return layout;
}

// The fixed last row of the routing list.
export const AUTOMATIC_ROW = 'Everything else: the cheapest reliable route, as before.';

export const ENCRYPTION_NOTE = 'SSL Fax encrypts the call, but it cannot confirm who answers at the other end, and the '
  + "call goes through as an ordinary fax if the other machine doesn't take SSL Fax.";

export const ALTERNATE_NOTE = 'An approved alternate number is one the recipient confirmed. When it is a toll-free '
  + 'number, the recipient pays for the call.';

export const LAYOUT_NOTE = 'Several pages on one sheet saves pages and call time. Faxbot stacks whole pages only as '
  + 'far as the receiving machine says it can print, and marks them so a receiving Faxbot can split them again.';
