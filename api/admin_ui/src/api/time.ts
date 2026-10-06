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

// A plain calendar date ("2026-10-03") in the reader's words, such as "3 October 2026"; the text unchanged otherwise.
export function formatLocalDate(day: string | null | undefined, fallback = '-'): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day ?? '');
  if (!match) return day || fallback;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

export function isPast(value: string | null | undefined, now = Date.now()): boolean {
  const date = parseServerTime(value);
  return date !== null && date.getTime() <= now;
}
