// One reading of GET /routing/costs for every place that shows spending (Tools → Delivery routes and the
// Dashboard card), so the two can never disagree: plans, charges, estimates, received calls and unmatched records.
import type { Money, ProviderCosts, ReceivedCosts, RouteCostsResponse } from '../../api/deliveryTypes';
import { providerLabel } from '../../providerLabels';
import { formatMoney, formatMoneyList } from './shared';

export const NO_PUBLISHED_PRICE = 'No published price; add your rate';

function add(totals: Map<string, number>, values: Money[] | undefined) {
  for (const value of values ?? []) totals.set(value.currency, (totals.get(value.currency) ?? 0) + Number(value.amount));
}

function moneyOf(totals: Map<string, number>): Money[] {
  return [...totals].map(([currency, amount]) => ({ currency, amount: String(Number(amount.toFixed(6))) }));
}

// What a route cost: the server's total, or charges plus estimates for faxes not billed yet (older servers).
export function sentTotal(provider: ProviderCosts): Money[] {
  if (provider.total_cost) return provider.total_cost;
  const totals = new Map<string, number>();
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
  if (total.length === 0 || (provider.priced === false && provider.reported_cost.length === 0)) return NO_PUBLISHED_PRICE;
  return formatMoneyList(total);
}

export function receivedLabel(entry: ReceivedCosts): string {
  return `Received on your ${providerLabel(entry.provider_id)}${entry.carrier ? ` · ${entry.carrier}` : ''}`;
}

export function receivedCost(entry: ReceivedCosts): string {
  return formatMoneyList(entry.total_cost ?? entry.reported_cost, 'Not billed yet');
}

export interface SpendingLine {
  key: string;
  label: string;
  value: string;
}

export function spendingLines(costs: Pick<RouteCostsResponse, 'providers' | 'received'>): SpendingLine[] {
  return [
    ...costs.providers.map((provider) => ({ key: `sent-${provider.provider_id}`, label: providerLabel(provider.provider_id), value: sentCost(provider) })),
    ...(costs.received ?? []).map((entry) => ({ key: `received-${entry.carrier ?? entry.provider_id}`, label: receivedLabel(entry), value: receivedCost(entry) })),
  ];
}

// Everything spent in the period: the server's total when it sends one (it counts each plan fee once per 30 days).
export function spendingTotal(costs: Pick<RouteCostsResponse, 'providers' | 'received' | 'total_cost'>): Money[] {
  if (costs.total_cost) return costs.total_cost;
  const totals = new Map<string, number>();
  for (const provider of costs.providers) add(totals, sentTotal(provider));
  for (const entry of costs.received ?? []) add(totals, entry.total_cost ?? entry.reported_cost);
  return moneyOf(totals);
}
