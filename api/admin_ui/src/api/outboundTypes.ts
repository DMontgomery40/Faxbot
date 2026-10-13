// The live outbound board (GET /outbound/now): what is sending, waiting and needs a person now, how today went,
// how full the lines are, and how each big send is going. Every sentence, label and fix is the server's own product
// text; times are UTC and are shown in local time.

export type OutboundSection = 'sending' | 'waiting' | 'needs_you' | 'today' | 'lines' | 'engine' | 'groups';

export interface SendingItem {
  id: string;
  // Masked the way Sent masks numbers.
  to: string;
  recipient: string | null;
  pages: number;
  // Only when the route or engine really reports progress; never estimated.
  page: number | null;
  route: string;
  engine: string | null;
  started_at: string | null;
}

export interface WaitingReason {
  key: string;
  count: number;
  sentence: string;
}

export interface WaitingItem {
  id: string;
  to: string;
  recipient: string | null;
  pages: number;
  reason: string;
  sentence: string;
  since: string;
  until: string | null;
}

export type NeedsYouKind = 'no_route' | 'approval' | 'window' | 'dialing' | 'uncertain' | 'other';

export interface NeedsYouItem {
  id: string;
  kind: NeedsYouKind;
  to: string;
  recipient: string | null;
  pages: number;
  since: string;
  // A window hold's go-out time.
  until?: string | null;
  sentence: string;
  // What POST /routing/holds/{id}/approve|refuse|check-again need, for a fax a rule holds.
  hold: { id: string; version: number } | null;
  // Worked out by the server for this viewer: approval → approve, refuse; no route → send by another account,
  // check again, refuse; window or dialing → refuse; uncertain → settle (on Sent). None: the fax opens in Sent.
  actions: string[];
}

export interface FailureReason {
  key: string;
  label: string;
  count: number;
}

export interface OutboundLine {
  key: string;
  label: string;
  in_use: number;
  limit: number | null;
  incoming: number;
  sentence: string;
}

export interface OutboundEngine {
  audio: boolean;
  since: string | null;
  sentence: string;
  fix: string;
}

export interface OutboundGroup {
  who: string;
  total: number;
  delivered: number;
  sending: number;
  waiting: number;
  needs_you: number;
  failed: number;
}

export interface OutboundNow {
  as_of: string;
  not_checked: string[];
  sending: { count: number; items: SendingItem[] };
  waiting: { count: number; more: number; reasons: WaitingReason[]; items: WaitingItem[] };
  needs_you: { count: number; items: NeedsYouItem[] };
  today: { since: string; accepted: number; delivered: number; failed: number; failures: FailureReason[] };
  lines: OutboundLine[];
  engine: OutboundEngine | null;
  groups: OutboundGroup[];
}

// One answer from the board: a reading, or a busy server and how long it asked the console to wait (Retry-After).
export type OutboundNowReply =
  | { kind: 'ready'; board: OutboundNow }
  | { kind: 'busy'; retryAfterMs: number | null };
