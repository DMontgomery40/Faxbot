// Numbers → Sender identity (reply number) and Numbers → Blocked senders.

export interface ReplyCandidate {
  number: string;
  provider: string;
  kind: 'local' | 'toll_free' | 'international' | 'other';
  receives: boolean;
  mailbox: string | null;
  mailbox_id: string | null;
  price: string;
  avoid: boolean;
  typical_cost: string | null;
}

export interface ReplyCallerId {
  provider: string;
  shows: boolean;
  sentence: string;
  source_url?: string | null;
  read_on?: string | null;
}

export interface ReplyNumberView {
  // The organization's saved reply number; null when Faxbot chooses.
  number: string | null;
  // The number faxes show now, and where it comes from.
  shows: string | null;
  source: 'mailbox' | 'organization' | 'station' | 'automatic' | 'line';
  sentence: string;
  problems: string[];
  mailboxes: Array<{ mailbox_id: string; mailbox: string; number: string; problem: string | null }>;
  candidates: ReplyCandidate[];
  suggestion: (ReplyCandidate & { sentence: string }) | null;
  avoid: Array<{ number: string; sentence: string }>;
  caller_id: ReplyCallerId[];
  header_problem: string | null;
}

export interface BlockedSender {
  id: string;
  number: string;
  reason: string;
  added_by: string | null;
  added_at: string;
  expires_at: string;
  removed_at: string | null;
  removed_by: string | null;
  active: boolean;
  rejected_calls: number;
}

export interface RejectedCall {
  id: string;
  number: string;
  called: string | null;
  rejected_at: string;
  reason: string | null;
}

export interface BlockedSendersView {
  sentence: string;
  synced: boolean;
  entries: BlockedSender[];
  rejections: RejectedCall[];
}
