// The words and readings of the live outbound board (OutboundBoard.tsx): one place for every string it adds, and the
// pure helpers that pick a tile's number and line. Sentences from the server are shown as they are.
import { parseServerTime } from '../../api/time';
import type {
  NeedsYouItem, NeedsYouKind, OutboundLine, OutboundNow, SendingItem, WaitingReason,
} from '../../api/outboundTypes';

export const BOARD_TEXT = {
  title: 'Outgoing faxes',
  loading: 'Loading outgoing faxes',
  failedEmpty: "Couldn't check outgoing faxes just now. Faxbot tries again by itself.",
  busyEmpty: 'Faxbot is busy right now. The board checks again by itself shortly.',
  notChecked: "Couldn't check this just now",
  sendingTitle: 'Sending now',
  waitingTitle: 'Waiting',
  needsYouTitle: 'Needs you',
  todayTitle: 'Today',
  delivered: 'delivered',
  todayDelivered: 'Delivered',
  todayFailed: 'Failed',
  todayAccepted: 'Sent today',
  sendingEmpty: 'Nothing is sending right now.',
  waitingEmpty: 'Nothing is waiting.',
  needsYouEmpty: 'Nothing needs you.',
  todayEmpty: 'No faxes sent yet today.',
  failuresEmpty: 'Nothing failed today.',
  failuresTitle: "Why faxes didn't go through",
  linesTitle: 'Lines',
  lineFull: 'Full',
  reasonsTitle: 'Why they wait',
  firstInLine: 'First in line',
  groupsTitle: 'Big sends today',
  engineNotChecked: "Couldn't check the fax engine just now.",
  approve: 'Approve',
  sendByAccount: 'Send by another account',
  checkAgain: 'Check again',
  refuse: 'Refuse',
  openInSent: 'Open in Sent',
  noLongerHeld: 'This fax is no longer held. The board shows its latest state.',
  showWaiting: 'Show the faxes ready to send in the list below',
  showNeedsYou: 'Show the faxes that need review in the list below',
};

export const LEGEND: Array<{ key: 'delivered' | 'sending' | 'waiting' | 'needs_you' | 'failed'; label: string }> = [
  { key: 'delivered', label: 'Delivered' },
  { key: 'sending', label: 'Sending' },
  { key: 'waiting', label: 'Waiting' },
  { key: 'needs_you', label: 'Needs you' },
  { key: 'failed', label: 'Failed' },
];

const KIND_WORDS: Record<NeedsYouKind, string> = {
  no_route: 'with no route your rules allow',
  approval: 'waiting for approval',
  window: 'waiting for a time window',
  dialing: 'held by your dialing settings',
  uncertain: 'with an unknown result',
  other: 'to look at',
};

const plural = (count: number, one: string, many: string) => (count === 1 ? one : many);

// A board section the server could not read this time.
export function notChecked(board: OutboundNow, section: string): boolean {
  return board.not_checked.includes(section);
}

// The name shown for a fax: the recipient when known, else the masked number.
export function faxName(fax: { recipient: string | null; to: string }): string {
  return fax.recipient || fax.to;
}

// "page 2 of 4" only when the route reports progress, else "4 pages".
export function pagesText(fax: Pick<SendingItem, 'page' | 'pages'>): string {
  if (fax.page !== null && fax.page !== undefined && fax.pages) return `page ${fax.page} of ${fax.pages}`;
  return `${fax.pages} ${plural(fax.pages, 'page', 'pages')}`;
}

export function routeText(fax: Pick<SendingItem, 'route' | 'engine'>): string {
  return fax.engine ? `${fax.route}, ${fax.engine}` : fax.route;
}

// Time on the line, "2:05" or "1:02:05".
export function durationText(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

export function onTheLineText(startedAt: string | null, now: number): string | null {
  const start = parseServerTime(startedAt);
  return start ? `${durationText(now - start.getTime())} on the line` : null;
}

// A local time, "14:30"; with the weekday when it is not today ("Wed 08:00").
export function whenText(value: string | null | undefined, now: number): string {
  const date = parseServerTime(value);
  if (!date) return '';
  const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return date.toDateString() === new Date(now).toDateString()
    ? time : `${date.toLocaleDateString(undefined, { weekday: 'short' })} ${time}`;
}

export function checkedText(at: number): string {
  return `Checked ${new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}.`;
}

export function busyText(at: number): string {
  return `Faxbot is busy, so this is what it read at ${new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}. It checks again by itself.`;
}

export function failedText(at: number): string {
  return `Couldn't check just now, so this is what Faxbot read at ${new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}. It tries again by itself.`;
}

// A line at its limit (a limit is known and every call in use).
export function lineFull(line: OutboundLine): boolean {
  return line.limit !== null && line.limit > 0 && line.in_use >= line.limit;
}

export function inUseText(line: OutboundLine): string {
  return line.limit !== null ? `${line.in_use} of ${line.limit} in use` : `${line.in_use} in use`;
}

// -- each tile's one short line ------------------------------------------------------------------------

export function sendingLine(board: OutboundNow): string {
  const { count, items } = board.sending;
  if (count === 0) return BOARD_TEXT.sendingEmpty;
  const first = items[0];
  if (!first) return `${count} ${plural(count, 'fax', 'faxes')} on the line`;
  if (count === 1) return `${faxName(first)}, ${pagesText(first)}`;
  return `${faxName(first)} and ${count - 1} more`;
}

// Full lines first, since that is why most faxes wait; else the biggest reason.
export function waitingLine(board: OutboundNow): string {
  if (board.waiting.count === 0) return BOARD_TEXT.waitingEmpty;
  const full = notChecked(board, 'lines') ? [] : board.lines.filter(lineFull);
  if (full.length === 1) return `${full[0].label} is full: ${full[0].in_use} of ${full[0].limit} calls in use.`;
  if (full.length > 1) return `${full.length} lines are full. Faxes wait for a free line.`;
  const top = board.waiting.reasons.reduce<WaitingReason | null>(
    (best, reason) => (reason.count > 0 && (!best || reason.count > best.count) ? reason : best), null);
  return top ? top.sentence : `${board.waiting.count} waiting to go`;
}

// The two commonest kinds among the listed faxes, then how many more: "2 waiting for approval, 1 with an unknown
// result, and 3 more".
export function needsYouLine(board: OutboundNow): string {
  const { count, items } = board.needs_you;
  if (count === 0) return BOARD_TEXT.needsYouEmpty;
  const kinds = new Map<NeedsYouKind, number>();
  items.forEach((item) => kinds.set(item.kind, (kinds.get(item.kind) ?? 0) + 1));
  const top = [...kinds.entries()].sort((a, b) => b[1] - a[1]).slice(0, 2);
  if (top.length === 0) return `${count} ${plural(count, 'fax needs', 'faxes need')} a decision`;
  const rest = count - top.reduce((sum, [, n]) => sum + n, 0);
  const parts = top.map(([kind, n]) => `${n} ${KIND_WORDS[kind] ?? KIND_WORDS.other}`);
  return rest > 0 ? `${parts.join(', ')}, and ${rest} more` : parts.join(', ');
}

export function todayLine(board: OutboundNow): string {
  const { accepted, delivered, failed } = board.today;
  if (accepted === 0 && failed === 0 && delivered === 0) return BOARD_TEXT.todayEmpty;
  const failedPart = failed === 0 ? 'None failed' : `${failed} failed`;
  // Faxes accepted before midnight can still be delivered or fail today.
  return accepted === 0 ? `${failedPart} today.` : `${failedPart}, of ${accepted} sent today.`;
}

// -- lists -----------------------------------------------------------------------------------------------

export function waitingMoreText(more: number): string {
  return `+${more} more further back in line.`;
}

export function shownOfText(shown: number, count: number): string {
  return `Showing the first ${shown} of ${count}.`;
}

export function untilText(value: string | null | undefined, now: number): string | null {
  return value ? `until ${whenText(value, now)}` : null;
}

export function goesOutText(value: string | null | undefined, now: number): string | null {
  return value ? `Goes out at ${whenText(value, now)}.` : null;
}

export function sinceText(value: string, now: number): string {
  const when = whenText(value, now);
  return when ? `Since ${when}` : '';
}

export function deliveredOfText(group: { delivered: number; total: number }): string {
  return `${group.delivered} of ${group.total} delivered`;
}

// Approve and the other decisions on a needs-you fax, in the server's order for this viewer.
export const ACTION_LABEL: Record<string, string> = {
  approve: BOARD_TEXT.approve,
  send_by_account: BOARD_TEXT.sendByAccount,
  check_again: BOARD_TEXT.checkAgain,
  refuse: BOARD_TEXT.refuse,
  settle: BOARD_TEXT.openInSent,
};

export function decisionNotice(action: 'approve' | 'check_again' | 'refuse' | 'send_by_account', item: NeedsYouItem,
  account?: string): string {
  const name = faxName(item);
  switch (action) {
    case 'approve': return `Approved. The fax to ${name} is no longer held.`;
    case 'check_again': return `Faxbot checked the routes for the fax to ${name} again.`;
    case 'refuse': return `Refused. Nothing was sent to ${name}.`;
    default: return `Approved. The fax to ${name} goes by ${account ?? 'the account you chose'}.`;
  }
}
