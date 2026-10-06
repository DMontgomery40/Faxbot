// Shared formatting for the delivery screens: money as money, minutes as minutes,
// and one plain sentence for a failed request.
import { Alert, Fade } from '@mui/material';
import { AdminAPIError, accessErrorMessage } from '../../api/client';
import type { Money, RecommendedRoute } from '../../api/deliveryTypes';

// An amount with no price at all: never shown as $0.
export const NOT_PRICED = 'Not priced yet';

const MICROS_PER_UNIT = 1_000_000n;

// The API's exact decimal string ("0.0095", "-1.50") as whole millionths, or null when it is not one.
export function toMicros(amount: string | null | undefined): bigint | null {
  const match = /^(-?)(\d+)(?:\.(\d{1,6}))?$/.exec(String(amount ?? '').trim());
  if (!match) return null;
  const micros = BigInt(match[2]) * MICROS_PER_UNIT + BigInt((match[3] ?? '').padEnd(6, '0'));
  return match[1] ? -micros : micros;
}

// Whole millionths back to the API's decimal string, trailing zeros trimmed to two places ("0.005", "1.50").
export function fromMicros(micros: bigint): string {
  const sign = micros < 0n ? '-' : '';
  const size = micros < 0n ? -micros : micros;
  const fraction = (size % MICROS_PER_UNIT).toString().padStart(6, '0').replace(/0+$/, '').padEnd(2, '0');
  return `${sign}${size / MICROS_PER_UNIT}.${fraction}`;
}

// The shown digits, worked out exactly from the decimal string as the faxbot command does: two places, or up to
// four under ten cents, rounded half up and trailing zeros trimmed ("0.005", "0.0013", "1.50").
function shownAmount(micros: bigint): { text: string; places: number } {
  const size = micros < 0n ? -micros : micros;
  const places = size !== 0n && size < 100_000n ? 4 : 2;
  const step = 10n ** BigInt(6 - places);
  const rounded = (size + step / 2n) / step;  // half up, away from zero
  let digits = rounded.toString().padStart(places + 1, '0');
  let shown = places;
  while (shown > 2 && digits.endsWith('0')) {
    digits = digits.slice(0, -1);
    shown -= 1;
  }
  const whole = digits.slice(0, digits.length - shown);
  return { text: `${micros < 0n && rounded > 0n ? '-' : ''}${whole}.${digits.slice(-shown)}`, places: shown };
}

export function formatMoney(value: Money | null | undefined): string {
  if (!value) return '-';
  const micros = toMicros(value.amount);
  if (micros === null) return '-';
  const { text, places } = shownAmount(micros);
  try {
    // The digits are already exact; the browser adds only the currency symbol and its own separators.
    return new Intl.NumberFormat(undefined, {
      style: 'currency', currency: value.currency, minimumFractionDigits: places, maximumFractionDigits: places,
    }).format(Number(text));
  } catch {
    return `${text} ${value.currency}`;
  }
}

// Several currencies joined with "+"; an empty list is an unknown amount, so the default says so, never $0.
export function formatMoneyList(values: Money[] | null | undefined, empty = NOT_PRICED): string {
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
  return (toMicros(amount) ?? 0n) > 0n ? `${formatMoney({ amount, currency })} per ${unit}` : null;
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
