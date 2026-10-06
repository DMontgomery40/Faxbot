// Plain sentences for the Work screen, in the reader's local time.
import { parseServerTime } from '../../api/time';
import type { WorkAction, WorkItem } from '../../api/types';

// A short local time for sentences, such as "3 Oct, 14:05".
export function shortTime(value: string | null | undefined): string {
  const date = parseServerTime(value);
  if (!date) return '';
  return date.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// The server writes each state as one sentence; a time in it is shown here in local time.
export function workStateSentence(item: Pick<WorkItem, 'state_key' | 'state_text' | 'due_at' | 'owner'>): string {
  if (item.state_key === 'assigned' && item.due_at) {
    return `Assigned to ${item.owner?.name || 'someone'}; acknowledge by ${shortTime(item.due_at)}.`;
  }
  return item.state_text;
}

export function duplicateSentence(item: Pick<WorkItem, 'duplicate_of'>): string | null {
  return item.duplicate_of ? `Same document as the one received ${shortTime(item.duplicate_of.available_at)}.` : null;
}

export function targetLabel(hours: number | null | undefined, inherited = 'Installation target'): string {
  if (hours === null || hours === undefined) return inherited;
  if (hours === 0) return 'No target';
  return hours === 1 ? '1 hour' : `${hours} hours`;
}

export const can = (item: Pick<WorkItem, 'actions'>, action: WorkAction) => item.actions.includes(action);

export function maskNumber(phone?: string | null): string {
  if (!phone || phone.length < 4) return '****';
  return '*'.repeat(phone.length - 4) + phone.slice(-4);
}

export const OPERATIONAL_TARGET = "This is your team's operational target, not a legal deadline.";
