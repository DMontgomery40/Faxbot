// One reading of GET /routing/costs for every place that shows spending (Tools → Delivery routes and the
// Dashboard card), so the two can never disagree: plans, charges, estimates, received calls and unmatched records.
import type { Money, ProviderCosts, ReceivedCosts, RouteCostsResponse } from '../../api/deliveryTypes';
import { providerLabel } from '../../providerLabels';
import { NOT_PRICED, formatMoney, formatMoneyList, fromMicros, toMicros } from './shared';

export const NO_PUBLISHED_PRICE = 'No published price; add your rate';
// A fax or call with no charge and no estimate: never shown or totalled as $0.
export { NOT_PRICED };

function countOf(count: number, unit: 'fax' | 'call'): string {
  return `${count} ${count === 1 ? unit : unit === 'fax' ? 'faxes' : 'calls'}`;
}

// "2 faxes not priced yet", or null when every one has a price.
export function notPriced(count: number | undefined, unit: 'fax' | 'call'): string | null {
  return count ? `${countOf(count, unit)} not priced yet` : null;
}

// What a line leaves out: "2 faxes not priced yet, 1 never priced in full". Never priced in full: the carrier
// priced part of the call by the give-up time and never the rest, so only that part is in the amount.
export function openCounts(notYet: number | undefined, never: number | undefined, unit: 'fax' | 'call'): string | null {
  const yet = notPriced(notYet, unit);
  if (!never) return yet;
  return yet ? `${yet}, ${never} never priced in full` : `${countOf(never, unit)} never priced in full`;
}

// The Spending card's sentence for those calls; `faxbot costs spending` prints the same.
export function neverPricedSentence(carrier: string | null | undefined, count: number, unit: 'fax' | 'call'): string {
  return `${carrier ?? 'Your carrier'} never priced ${countOf(count, unit)} in full; only `
    + `${count === 1 ? 'its priced part is' : 'their priced parts are'} in the total.`;
}

// Exact sums in millionths, as the server keeps them; an amount that is not a decimal adds nothing.
function add(totals: Map<string, bigint>, values: Money[] | undefined) {
  for (const value of values ?? []) {
    const micros = toMicros(value.amount);
    if (micros !== null) totals.set(value.currency, (totals.get(value.currency) ?? 0n) + micros);
  }
}

function moneyOf(totals: Map<string, bigint>): Money[] {
  return [...totals].map(([currency, micros]) => ({ currency, amount: fromMicros(micros) }));
}

// What a route cost: the server's total, or charges plus estimates for faxes not billed yet (older servers).
export function sentTotal(provider: ProviderCosts): Money[] {
  if (provider.total_cost) return provider.total_cost;
  const totals = new Map<string, bigint>();
  add(totals, provider.reported_cost);
  add(totals, provider.estimated_cost_not_reported ?? (provider.reported_cost.length ? [] : provider.estimated_cost));
  return moneyOf(totals);
}

export function planSentence(plan: NonNullable<ProviderCosts['plan']>): string {
  const fee = plan.monthly_fee_text ?? formatMoney(plan.monthly_fee);
  return `Included in your ${plan.label} plan (${fee} a month)`;
}

// The one phrase for a sending route's cost.
export function sentCost(provider: ProviderCosts): string {
  if (provider.plan) return planSentence(provider.plan);
  const total = sentTotal(provider);
  if (provider.priced === false && provider.reported_cost.length === 0) return NO_PUBLISHED_PRICE;
  if (total.length === 0) return (provider.attempts_not_priced ?? 0) > 0 ? NOT_PRICED : NO_PUBLISHED_PRICE;
  return formatMoneyList(total);
}

// "Received through Telnyx"; the carrier is added only when it is not already the name.
export function receivedLabel(entry: ReceivedCosts): string {
  return `Received through ${withCarrier(providerLabel(entry.provider_id), entry.carrier)}`;
}

export function withCarrier(name: string, carrier: string | null | undefined): string {
  return carrier && carrier !== name ? `${name} · ${carrier}` : name;
}

export function receivedCost(entry: ReceivedCosts): string {
  return formatMoneyList(entry.total_cost ?? entry.reported_cost, entry.calls_not_priced ? NOT_PRICED : 'Not billed yet');
}

// An amount with what it leaves out: "$0.005, 2 faxes not priced yet". A line that already says it has no price
// gets no count.
function withNotPriced(value: string, open: string | null): string {
  return open && value !== NO_PUBLISHED_PRICE && value !== NOT_PRICED ? `${value}, ${open}` : value;
}

export interface SpendingLine {
  key: string;
  label: string;
  value: string;
}

export function spendingLines(costs: Pick<RouteCostsResponse, 'providers' | 'received'>): SpendingLine[] {
  return [
    ...costs.providers.map((provider) => ({ key: `sent-${provider.provider_id}`, label: providerLabel(provider.provider_id),
      value: withNotPriced(sentCost(provider), openCounts(provider.attempts_not_priced, provider.attempts_never_priced, 'fax')) })),
    ...(costs.received ?? []).map((entry) => ({ key: `received-${entry.carrier ?? entry.provider_id}`, label: receivedLabel(entry),
      value: withNotPriced(receivedCost(entry), openCounts(entry.calls_not_priced, entry.calls_never_priced, 'call')) })),
  ];
}

// "2 faxes and 1 call not priced yet" across every route (none of them is in the total), then "1 call never
// priced in full" (only its priced part is), or null.
export function notPricedTotal(costs: Pick<RouteCostsResponse, 'providers' | 'received'>): string | null {
  const counted = (faxes: number, calls: number, words: string) => {
    const parts = [...(faxes ? [countOf(faxes, 'fax')] : []), ...(calls ? [countOf(calls, 'call')] : [])];
    return parts.length ? [`${parts.join(' and ')} ${words}`] : [];
  };
  const sum = (values: (number | undefined)[]) => values.reduce<number>((total, value) => total + (value ?? 0), 0);
  const received = costs.received ?? [];
  const phrases = [
    ...counted(sum(costs.providers.map((provider) => provider.attempts_not_priced)),
      sum(received.map((entry) => entry.calls_not_priced)), 'not priced yet'),
    ...counted(sum(costs.providers.map((provider) => provider.attempts_never_priced)),
      sum(received.map((entry) => entry.calls_never_priced)), 'never priced in full'),
  ];
  return phrases.length ? phrases.join(', ') : null;
}

// The Total line: the known total and what it leaves out, never a $0 for faxes with no price.
export function spendingTotalText(costs: Pick<RouteCostsResponse, 'providers' | 'received' | 'total_cost'>): string {
  const total = spendingTotal(costs);
  const open = notPricedTotal(costs);
  if (total.length === 0) return open ? open.charAt(0).toUpperCase() + open.slice(1) : NO_PUBLISHED_PRICE;
  return withNotPriced(formatMoneyList(total), open);
}

// Everything spent in the period: the server's total when it sends one (it counts each plan fee once per 30 days).
export function spendingTotal(costs: Pick<RouteCostsResponse, 'providers' | 'received' | 'total_cost'>): Money[] {
  if (costs.total_cost) return costs.total_cost;
  const totals = new Map<string, bigint>();
  for (const provider of costs.providers) add(totals, sentTotal(provider));
  for (const entry of costs.received ?? []) add(totals, entry.total_cost ?? entry.reported_cost);
  return moneyOf(totals);
}
