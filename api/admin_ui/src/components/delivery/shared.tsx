// Shared formatting for the delivery screens: money as money, minutes as minutes,
// and one plain sentence for a failed request.
import { Alert, Fade } from '@mui/material';
import { AdminAPIError, accessErrorMessage } from '../../api/client';
import type { Money, RecommendedRoute } from '../../api/deliveryTypes';

export function formatMoney(value: Money | null | undefined): string {
  if (!value) return '-';
  const amount = Number(value.amount);
  if (!Number.isFinite(amount)) return '-';
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency', currency: value.currency, minimumFractionDigits: 2, maximumFractionDigits: amount !== 0 && Math.abs(amount) < 0.1 ? 4 : 2,
    }).format(amount);
  } catch {
    return `${value.amount} ${value.currency}`;
  }
}

export function formatMoneyList(values: Money[] | null | undefined, empty = 'None yet'): string {
  if (!values || values.length === 0) return empty;
  return values.map(formatMoney).join(' + ');
}

// What a fax costs on a route, in the route's own units: "About $0.01 for this 2-page fax
// ($0.005 a minute, at least 1 minute)." or, with no page count, "About $0.07 a page."
// Nothing for a route in a flat plan; its explanation already says so.
export function routeCostSentence(route: RecommendedRoute, pages: number | null): string | null {
  if (route.included_in_plan) return null;
  if (pages && route.pages === pages && route.estimated_cost) {
    return `About ${formatMoney(route.estimated_cost)} for this ${pages}-page fax${route.rate ? ` (${route.rate})` : ''}.`;
  }
  return route.rate ? `About ${route.rate}.` : null;
}

export function formatRate(amount: string, currency: string, unit: string): string | null {
  return Number(amount) > 0 ? `${formatMoney({ amount, currency })} per ${unit}` : null;
}

export function formatMinutes(minutes: number): string {
  if (minutes === 1) return '1 minute';
  return `${minutes.toLocaleString(undefined, { maximumFractionDigits: 1 })} minutes`;
}

export function formatPercent(value: number | null): string {
  return value === null ? 'No results yet' : `${value}% delivered`;
}

// The delivery routes, intake and direct delivery APIs answer 400 and 409
// with operator sentences written for this screen, so those are shown as-is.
export function deliveryErrorMessage(error: unknown): string {
  if (error instanceof AdminAPIError && (error.status === 400 || error.status === 409) && error.detail
      && /^[A-Z][^<>{}]{3,240}[.!?]$/.test(error.detail)) {
    return error.detail;
  }
  if (error instanceof AdminAPIError && error.status === 409) return 'This changed since you opened it. Reload and try again.';
  return accessErrorMessage(error);
}

export function DeliveryError({ error, onClose }: { error: unknown; onClose?: () => void }) {
  if (!error) return null;
  return (
    <Fade in>
      <Alert severity="error" sx={{ mb: 3, borderRadius: 2 }} onClose={onClose}>{deliveryErrorMessage(error)}</Alert>
    </Fade>
  );
}

export function Notice({ message, onClose }: { message: string | null; onClose: () => void }) {
  if (!message) return null;
  return (
    <Fade in>
      <Alert severity="success" sx={{ mb: 3, borderRadius: 2 }} onClose={onClose}>{message}</Alert>
    </Fade>
  );
}
