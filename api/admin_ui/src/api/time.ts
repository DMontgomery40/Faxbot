// The server sends and accepts naive UTC ISO strings (no "Z").

export function parseServerTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const date = new Date(hasZone ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatServerTime(value: string | null | undefined, fallback = '-'): string {
  const date = parseServerTime(value);
  return date ? date.toLocaleString() : fallback;
}

export function toServerTime(date: Date): string {
  return date.toISOString().replace(/Z$/, '');
}

// A local calendar date (yyyy-mm-dd) means "through the end of that day".
export function endOfLocalDay(day: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const date = new Date(`${day}T23:59:59`);
  return Number.isNaN(date.getTime()) ? null : toServerTime(date);
}

// The local calendar date of a server timestamp, for a date input.
export function localDay(value: string | null | undefined): string {
  const date = parseServerTime(value);
  if (!date) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function isPast(value: string | null | undefined, now = Date.now()): boolean {
  const date = parseServerTime(value);
  return date !== null && date.getTime() <= now;
}
