// Needs attention: what waits for a person, from the reads Faxbot already has, grouped by the action it
// needs. Each source keeps its own state, so a source this account may not read, or one that failed, is
// named instead of read as "nothing here". `faxbot overview` (api/app/cli/commands/overview.py) computes
// the same items from the same answers; overviewAttention.json holds both surfaces to them.
import AdminAPIClient, { AdminAPIError, isNotAvailable } from '../../api/client';
import type { HealthStatus, WorkCounts } from '../../api/types';
import type { IntakeCounts, RouteCostsResponse } from '../../api/deliveryTypes';
import type { ExpectedCounts } from '../../api/expectedTypes';
import type { SipNetworkReport } from '../../api/networkTypes';
import type { AdminDestination } from '../../navigation';
import { rulesApiFor, type Hold } from '../ProviderRulesApi';
import { expectedApi } from '../expected/expectedApi';

// One read and when it answered (milliseconds); a read that was refused or failed keeps that state.
export type Loaded<T> =
  | { kind: 'loading' }
  | { kind: 'ready'; data: T; at: number }
  | { kind: 'denied' | 'unavailable' | 'error'; at: number };

export async function settle<T>(request: Promise<T>, now: () => number = Date.now): Promise<Loaded<T>> {
  try {
    const data = await request;
    return { kind: 'ready', data, at: now() };
  } catch (error) {
    if (error instanceof AdminAPIError && (error.status === 401 || error.status === 403)) return { kind: 'denied', at: now() };
    return { kind: isNotAvailable(error) ? 'unavailable' : 'error', at: now() };
  }
}

export interface AttentionSources {
  // Readiness and the sent-fax counts (failed in the last day, uncertain); polled with the Overview.
  health: Loaded<HealthStatus>;
  holds: Loaded<Hold[]>;
  work: Loaded<WorkCounts>;
  intake: Loaded<IntakeCounts>;
  expected: Loaded<ExpectedCounts>;
  costs: Loaded<RouteCostsResponse>;
  network: Loaded<SipNetworkReport>;
}

export type AttentionSource = keyof AttentionSources;

const SOURCES: AttentionSource[] = ['health', 'holds', 'work', 'intake', 'expected', 'costs', 'network'];

// What each source covers, in the sentences that name a source Faxbot could not check.
export const SOURCE_NAMES: Record<AttentionSource, string> = {
  health: "Faxbot's status and sent faxes",
  holds: 'held faxes',
  work: 'owners of received faxes',
  intake: 'email delivery',
  expected: 'expected faxes',
  costs: 'carrier charges',
  network: 'the network check',
};

// The sources the Overview reads on entry and on Refresh; Faxbot's status comes from its own poll.
export async function loadAttentionSources(client: AdminAPIClient): Promise<Omit<AttentionSources, 'health'>> {
  const [holds, work, intake, expected, costs, network] = await Promise.all([
    settle(rulesApiFor(client).holds().then((result) => result.holds)),
    settle(client.workCounts()),
    settle(client.listIntakeItems({ limit: 1 }).then((result) => result.counts)),
    settle(expectedApi(client).counts()),
    settle(client.getRouteCosts()),
    settle(client.getSipNetwork()),
  ]);
  return { holds, work, intake, expected, costs, network };
}

export type AttentionGroup = 'serious' | 'decide' | 'owner' | 'failed' | 'check';

export const GROUP_TITLES: Record<AttentionGroup, string> = {
  serious: 'Serious problems',
  decide: 'Waiting for your decision',
  owner: 'Waiting for an owner, or overdue',
  failed: 'Did not go through',
  check: 'To check',
};

const GROUP_ORDER: AttentionGroup[] = ['serious', 'decide', 'owner', 'failed', 'check'];

// One thing that needs a person: how many, and the exact list that handles it.
export interface AttentionItem {
  key: string;
  group: AttentionGroup;
  label: string;
  count: number | null;
  detail: string | null;
  // A provider or the fax engine is down, or a sent fax's outcome is uncertain.
  serious: boolean;
  destination: AdminDestination;
}

export interface AttentionView {
  items: AttentionItem[];
  groups: Array<{ key: AttentionGroup; title: string; items: AttentionItem[] }>;
  serious: boolean;
  loading: boolean;
  // Every source answered, so "Nothing needs attention" may be said.
  complete: boolean;
  denied: AttentionSource[];
  failed: AttentionSource[];
  // The sentence when nothing was found, and the sentences naming what could not be checked.
  empty: string | null;
  coverage: string[];
  // When the oldest answer used here came in (milliseconds), or null before any.
  checkedAt: number | null;
}

// An install with a provider in one direction only is judged by that direction.
export function notReadyFor(health: HealthStatus): 'send' | 'receive' | 'both' | null {
  const sending = !!health.backend && !health.backend_healthy;
  const receiving = !!health.receiving_backend && !health.receiving_ready;
  return sending && receiving ? 'both' : sending ? 'send' : receiving ? 'receive' : null;
}

export const NOT_READY_TEXT = {
  send: 'Faxbot is not ready to send faxes',
  receive: 'Faxbot is not ready to receive faxes',
  both: 'Faxbot is not ready to send or receive faxes',
} as const;

export function heldDetail(holds: Hold[]): string {
  const count = (kind: Hold['kind']) => holds.filter((hold) => hold.kind === kind).length;
  const parts = [
    count('approval') > 0 && `${count('approval')} waiting for approval`,
    count('no_route') > 0 && `${count('no_route')} with no route your rules allow`,
    count('window') > 0 && `${count('window')} waiting for a time window`,
  ].filter(Boolean);
  return `${parts.join(', ')}. Nothing has been sent for them.`;
}

function list(names: string[]): string {
  return names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

export function attentionView(sources: AttentionSources, { canSetUp = false }: { canSetUp?: boolean } = {}): AttentionView {
  const items: AttentionItem[] = [];
  const add = (item: Omit<AttentionItem, 'detail' | 'serious'> & Partial<Pick<AttentionItem, 'detail' | 'serious'>>) =>
    items.push({ detail: null, serious: false, ...item });
  const { health, holds, work, intake, expected, costs, network } = sources;

  if (health.kind === 'ready') {
    const status = health.data;
    if (!status.backend && !status.receiving_backend) {
      add({ key: 'no-provider', group: 'check', label: 'No fax provider is set up yet', count: null,
        destination: canSetUp ? 'setup' : 'diagnostics' });
    }
    const notReady = notReadyFor(status);
    if (notReady) {
      add({ key: 'not-ready', group: 'serious', label: NOT_READY_TEXT[notReady], count: null, serious: true,
        destination: 'system/diagnostics' });
    }
    if (status.jobs.reconciliation_required) {
      add({ key: 'uncertain', group: 'serious', label: 'Sent faxes with an uncertain result',
        count: status.jobs.reconciliation_required, serious: true,
        detail: 'Check your provider account before you send any of them again.',
        destination: 'faxes/sent?status=reconciliation_required' });
    }
    if (status.jobs.recent_failures) {
      add({ key: 'failed', group: 'failed', label: 'Sent faxes that failed in the last 24 hours',
        count: status.jobs.recent_failures, destination: 'faxes/sent?status=failed&since=24h' });
    }
  }
  if (holds.kind === 'ready' && holds.data.length > 0) {
    add({ key: 'held', group: 'decide', label: 'Faxes your rules are holding', count: holds.data.length,
      detail: heldDetail(holds.data), destination: 'faxes/sent?show=held' });
  }
  if (expected.kind === 'ready' && expected.data.proposed > 0) {
    add({ key: 'proposed', group: 'decide', label: 'Expected faxes with a possible match to confirm',
      count: expected.data.proposed, destination: 'faxes/expected?show=proposed' });
  }
  if (work.kind === 'ready' && work.data.unassigned > 0) {
    add({ key: 'unassigned', group: 'owner', label: 'Received faxes waiting for an owner', count: work.data.unassigned,
      destination: 'faxes/received?show=waiting' });
  }
  if (work.kind === 'ready' && work.data.overdue > 0) {
    add({ key: 'overdue', group: 'owner', label: 'Received faxes that are overdue', count: work.data.overdue,
      destination: 'faxes/received?show=overdue' });
  }
  if (expected.kind === 'ready' && expected.data.overdue > 0) {
    add({ key: 'expected-overdue', group: 'owner', label: 'Expected faxes that are overdue', count: expected.data.overdue,
      destination: 'faxes/expected?show=overdue' });
  }
  if (intake.kind === 'ready' && intake.data.failed > 0) {
    add({ key: 'not-delivered', group: 'failed', label: 'Received faxes not delivered by email', count: intake.data.failed,
      destination: 'faxes/received?show=not-delivered' });
  }
  if (costs.kind === 'ready') {
    const unrecorded = [...costs.data.providers, ...(costs.data.received ?? [])]
      .reduce((total, row) => total + (row.unrecorded_calls ?? 0), 0);
    if (unrecorded > 0) {
      add({ key: 'unrecorded', group: 'check', label: 'Carrier charges with no matching fax, last 30 days', count: unrecorded,
        destination: 'costs/spending' });
    }
  }
  // While Faxbot keeps fax over the internet off because of the network.
  if (network.kind === 'ready' && network.data.applies && network.data.action === 'turned_off' && !network.data.t38_enabled) {
    add({ key: 't38-network', group: 'check',
      label: 'One network change would let faxes go over the internet; faxes still go through meanwhile', count: null,
      destination: 'providers/trunk' });
  }

  const ordered = GROUP_ORDER.flatMap((group) => items.filter((item) => item.group === group));
  const states = SOURCES.map((source) => [source, sources[source]] as const);
  const denied = states.filter(([, state]) => state.kind === 'denied').map(([source]) => source);
  const failed = states.filter(([, state]) => state.kind === 'error' || state.kind === 'unavailable').map(([source]) => source);
  const loading = states.some(([, state]) => state.kind === 'loading');
  const complete = !loading && denied.length === 0 && failed.length === 0;
  const coverage = [
    denied.length > 0 && `Not available to this account: ${list(denied.map((source) => SOURCE_NAMES[source]))}.`,
    failed.length > 0 && `Could not check ${list(failed.map((source) => SOURCE_NAMES[source]))}. Select Refresh to try again.`,
  ].filter((sentence): sentence is string => Boolean(sentence));
  const times = states.flatMap(([, state]) => (state.kind === 'ready' ? [state.at] : []));
  return {
    items: ordered,
    groups: GROUP_ORDER.map((group) => ({ key: group, title: GROUP_TITLES[group], items: ordered.filter((item) => item.group === group) }))
      .filter((group) => group.items.length > 0),
    serious: ordered.some((item) => item.serious),
    loading,
    complete,
    denied,
    failed,
    empty: ordered.length > 0 || loading ? null
      : complete ? 'Nothing needs attention.' : 'Nothing needs attention in what Faxbot could check.',
    coverage,
    checkedAt: times.length > 0 ? Math.min(...times) : null,
  };
}
