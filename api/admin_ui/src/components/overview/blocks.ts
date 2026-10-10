// The Overview's blocks, as plain data: what Faxbot did here, the next improvements, and the everyday counts.
// Each block reads its own sources and has its own state and time, so a block that could not be read says so
// and a health poll never makes money or work figures look newer. `faxbot overview`
// (api/app/cli/commands/overview.py) builds the same lines; overviewBlocks.json holds both to them.
import type { HealthStatus, WorkCounts } from '../../api/types';
import type { IntakeCounts, Money, Savings, SendingRecommendations } from '../../api/deliveryTypes';
import type { Capabilities, CapabilityImprovementKind } from '../../api/capabilityTypes';
import type { ExpectedCounts } from '../../api/expectedTypes';
import type { FactAdvice } from '../../api/factAdviceTypes';
import type { Loaded } from './attention';

// A block whose newest answer is older than this says when it was last checked.
export const STALE_AFTER_MS = 10 * 60 * 1000;

export type BlockState = 'loading' | 'ready' | 'empty' | 'denied' | 'unavailable' | 'failed' | 'stale';

export const BLOCK_TEXT: Record<'denied' | 'unavailable' | 'failed', string> = {
  denied: 'Not available to this account.',
  unavailable: 'Not available on this server.',
  failed: 'Could not load this. Select Refresh to try again.',
};

// One block's state from its sources: the first that is still loading, refused or failed decides; otherwise it is
// ready (or empty), and stale once its oldest answer is older than STALE_AFTER_MS.
export function blockState(sources: Array<Loaded<unknown>>, { now, empty }: { now: number; empty: boolean })
  : { state: BlockState; at: number | null } {
  if (sources.some((source) => source.kind === 'loading')) return { state: 'loading', at: null };
  const times = sources.map((source) => (source as { at: number }).at);
  const at = times.length ? Math.min(...times) : null;
  if (sources.some((source) => source.kind === 'denied')) return { state: 'denied', at };
  if (sources.some((source) => source.kind === 'error')) return { state: 'failed', at };
  if (sources.some((source) => source.kind === 'unavailable')) return { state: 'unavailable', at };
  if (at !== null && now - at > STALE_AFTER_MS) return { state: 'stale', at };
  return { state: empty ? 'empty' : 'ready', at };
}

export function clockText(at: number): string {
  return new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

export function staleText(at: number): string {
  return `Last checked at ${clockText(at)}. Select Refresh to check again.`;
}

// -- What Faxbot is doing ------------------------------------------------------------------------------

// A capability that acted on faxes or calls here in the period, with the server's own count.
export interface UsedCapability {
  key: string;
  name: string;
  sentence: string;
  used: number;
  address: string;
}

export function usedCapabilities(capabilities: Capabilities): UsedCapability[] {
  return capabilities.outcomes.flatMap((outcome) => outcome.capabilities)
    .filter((capability) => capability.here.used > 0 && capability.here.sentence)
    .map((capability) => ({ key: capability.key, name: capability.name, sentence: capability.here.sentence as string,
      used: capability.here.used, address: capability.address }))
    .sort((a, b) => b.used - a.used);
}

export type ResultUnit = 'money' | 'pages' | 'seconds' | 'calls' | 'bytes';

// One unit of results and the parts it came from. Parts of one unit are listed, never added together: two parts
// can act on the same fax, and one total would count it twice. Money is the server's own total.
export interface ResultLine {
  unit: ResultUnit;
  label: string;
  // The server's sentence for money; the parts for the other units.
  sentence: string | null;
  parts: Array<{ key: string; label: string; value: string }>;
}

type Counted = Record<string, number | undefined>;

const UNIT_PARTS: Record<Exclude<ResultUnit, 'money'>, Array<[keyof Savings, string, string]>> = {
  pages: [['separator_pages', 'pages_saved', 'Fewer separator pages'], ['case_packets', 'pages_saved', 'Case packets'],
    ['packing', 'pages_saved', 'Dense pages'], ['encoding', 'pages_saved', 'Encoded pages'],
    ['continuation', 'pages_not_resent', 'Only the missing pages'],
    ['partner_repair', 'pages_not_resent', 'Missing pages to partners']],
  seconds: [['sslfax', 'seconds_saved', 'Faster pages'], ['packing', 'seconds_saved', 'Dense pages'],
    ['encoding', 'seconds_saved', 'Encoded pages'], ['fax_friendly', 'seconds_saved', 'Lighter shading'],
    ['coding', 'seconds_saved', 'Smallest page coding']],
  calls: [['sending_together', 'calls_saved', 'Sending together'], ['direct_delivery', 'calls_avoided', 'Direct delivery'],
    ['direct_fax_images', 'calls_avoided', 'Fax images to partners'],
    ['own_numbers', 'calls_avoided', 'Faxes to your own numbers']],
  bytes: [['direct_bytes', 'bytes_saved', 'Send once and reuse']],
};

const UNIT_LABELS: Record<ResultUnit, string> = {
  money: 'Money (estimate)', pages: 'Pages not sent', seconds: 'Call time saved', calls: 'Calls avoided',
  bytes: 'Data not sent again',
};

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function durationText(seconds: number): string {
  const whole = Math.round(seconds);
  if (whole < 60) return plural(whole, 'second');
  const minutes = Math.round(whole / 60);
  if (minutes < 60) return plural(minutes, 'minute');
  return `${plural(Math.floor(minutes / 60), 'hour')} ${plural(minutes % 60, 'minute')}`;
}

export function bytesText(bytes: number): string {
  if (bytes < 1000) return plural(bytes, 'byte');
  if (bytes < 1_000_000) return `${(bytes / 1000).toFixed(1)} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

function unitValue(unit: ResultUnit, value: number): string {
  if (unit === 'pages') return plural(value, 'page');
  if (unit === 'seconds') return durationText(value);
  if (unit === 'calls') return plural(value, 'call');
  return bytesText(value);
}

function hasMoney(amounts: Money[] | undefined): boolean {
  return (amounts ?? []).some((amount) => Number(amount.amount) !== 0);
}

export function resultLines(savings: Savings): ResultLine[] {
  const lines: ResultLine[] = [];
  // Only a real amount is shown: no faxes in the period means no money line at all.
  if (hasMoney(savings.total_saved)) {
    lines.push({ unit: 'money', label: UNIT_LABELS.money, sentence: savings.total_sentence ?? null, parts: [] });
  }
  for (const unit of ['pages', 'seconds', 'calls', 'bytes'] as const) {
    const parts = UNIT_PARTS[unit].flatMap(([part, field, label]) => {
      const value = ((savings[part] ?? {}) as Counted)[field] ?? 0;
      return value > 0 ? [{ key: String(part), label, value: unitValue(unit, value) }] : [];
    });
    if (parts.length) lines.push({ unit, label: UNIT_LABELS[unit], sentence: null, parts });
  }
  return lines;
}

// For a new installation: the connections that would make more capabilities work, and which ones.
export interface ConnectNext {
  address: string;
  label: string;
  capabilities: string[];
}

export function connectNext(capabilities: Capabilities): ConnectNext[] {
  const found = new Map<string, ConnectNext>();
  for (const capability of capabilities.outcomes.flatMap((outcome) => outcome.capabilities)) {
    const missing = capability.prerequisites.filter((prerequisite) => !prerequisite.met);
    // Only those a connection alone would make work: nothing else is missing.
    if (missing.length === 0 || missing.some((prerequisite) => prerequisite.kind !== 'connection')) continue;
    for (const prerequisite of missing) {
      const entry = found.get(prerequisite.address)
        ?? { address: prerequisite.address, label: prerequisite.address_label, capabilities: [] };
      if (!entry.capabilities.includes(capability.name)) entry.capabilities.push(capability.name);
      found.set(prerequisite.address, entry);
    }
  }
  return [...found.values()].sort((a, b) => b.capabilities.length - a.capabilities.length).slice(0, 3);
}

// -- Next improvements -----------------------------------------------------------------------------------

export const IMPROVEMENT_LABELS: Record<CapabilityImprovementKind, string> = {
  now: 'You can do this now',
  fact: 'Needs a fact',
  agreement: "Needs a recipient's agreement",
  experimental: 'Experimental',
};

const KIND_ORDER: CapabilityImprovementKind[] = ['now', 'fact', 'agreement', 'experimental'];

// The most next improvements the Overview lists; every capability is on the Capabilities page.
export const IMPROVEMENTS_SHOWN = 8;

export interface Improvement {
  key: string;
  kind: CapabilityImprovementKind;
  kindLabel: string;
  title: string;
  sentence: string;
  // Its capability's page, or the opportunity's section.
  destination: string;
  // The faxbot command that does the same, or null where none does (an automatic capability).
  command: string | null;
}

export function nextImprovements({ capabilities, sending, facts }: {
  capabilities: Capabilities | null;
  sending: SendingRecommendations | null;
  facts: FactAdvice | null;
}): Improvement[] {
  const items: Improvement[] = [];
  for (const capability of (capabilities?.outcomes ?? []).flatMap((outcome) => outcome.capabilities)) {
    if (!capability.ready || !capability.improvement) continue;
    items.push({ key: `capability-${capability.key}`, kind: capability.improvement.kind,
      kindLabel: capability.improvement.label, title: capability.name, sentence: capability.sentence,
      destination: capability.address, command: capability.setting.command });
  }
  for (const item of (sending?.items ?? []).slice(0, 3)) {
    items.push({ key: `sending-${item.number}`, kind: 'now', kindLabel: IMPROVEMENT_LABELS.now,
      title: `A cheaper route to ${item.display_name ?? item.number}`, sentence: item.sentence,
      destination: 'savings/opportunities?section=sending', command: 'faxbot costs recommendations sending' });
  }
  const factRows = (facts?.recipients ?? []).flatMap((recipient) => recipient.facts
    .filter((row) => !row.realized)
    .map((row) => ({ recipient, row })));
  for (const { recipient, row } of factRows.slice(0, 3)) {
    const kind: CapabilityImprovementKind = row.kind === 'authorization' ? 'agreement' : 'fact';
    items.push({ key: `fact-${recipient.number}-${row.fact}`, kind, kindLabel: IMPROVEMENT_LABELS[kind],
      title: `${row.title} (${recipient.name ?? recipient.display})`, sentence: row.step,
      destination: 'savings/facts', command: 'faxbot costs advice' });
  }
  return KIND_ORDER.flatMap((kind) => items.filter((item) => item.kind === kind)).slice(0, IMPROVEMENTS_SHOWN);
}

// -- Everyday faxes ---------------------------------------------------------------------------------------

export interface EverydayLine {
  key: 'received' | 'sent' | 'expected';
  label: string;
  // One sentence with the counts, or what this account cannot see.
  text: string;
  destination: string;
}

export function everydayLines({ health, work, expected, intake }: {
  health: Loaded<HealthStatus>;
  work: Loaded<WorkCounts>;
  expected: Loaded<ExpectedCounts>;
  intake: Loaded<IntakeCounts>;
}): EverydayLine[] {
  const unreadable = (state: Loaded<unknown>) => (state.kind === 'loading' ? 'Checking…'
    : state.kind === 'denied' ? BLOCK_TEXT.denied : state.kind === 'unavailable' ? BLOCK_TEXT.unavailable
      : 'Could not check.');
  let received: string;
  if (work.kind === 'ready') {
    const parts = [plural(work.data.open, 'open fax', 'open faxes'),
      work.data.unassigned > 0 && `${work.data.unassigned} without an owner`,
      intake.kind === 'ready' && intake.data.received + intake.data.sending > 0
        && `${intake.data.received + intake.data.sending} waiting for email delivery`].filter(Boolean);
    received = `${parts.join(', ')}.`;
  } else {
    received = unreadable(work);
  }
  let sent: string;
  if (health.kind === 'ready') {
    const jobs = health.data.jobs;
    // An installation that only receives has nothing to send, and that is not a fault.
    sent = !health.data.backend
      ? (health.data.receiving_backend ? 'This installation receives faxes only; sending is not set up.'
        : 'Sending is not set up yet.')
      : `${plural(jobs.queued, 'fax', 'faxes')} waiting to send, ${jobs.in_progress} sending now.`;
  } else {
    sent = unreadable(health);
  }
  const waiting = expected.kind === 'ready'
    ? `${plural(expected.data.waiting, 'expected fax', 'expected faxes')} waiting.` : unreadable(expected);
  return [
    { key: 'received', label: 'Received', text: received, destination: 'faxes/received' },
    { key: 'sent', label: 'Sent', text: sent, destination: 'faxes/sent' },
    { key: 'expected', label: 'Expected', text: waiting, destination: 'faxes/expected' },
  ];
}

