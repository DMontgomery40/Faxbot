// Delivery setup → Sending identity (reply number) and Delivery setup → Blocked senders.

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
  // The received fax it was marked from, when it came from one.
  inbound_id: string | null;
}

export interface RejectedCall {
  id: string;
  number: string;
  called: string | null;
  rejected_at: string;
  entry_id: string | null;
}

export interface BlockedSendersView {
  sentence: string;
  synced: boolean;
  entries: BlockedSender[];
  rejections: RejectedCall[];
}

// Recipients → Details, "Their fax machine" (patch 0004's frames and what Faxbot learned).
export interface FaxMachineCall {
  when: string;
  direction: 'in' | 'out';
  mode: string | null;
  status: string | null;
  rate_first: number | null;
  rate_lowest: number | null;
  trainings: number | null;
  failures_to_train: number | null;
  t38_after_ms: number | null;
  t38_by: string | null;
  iaf: string | null;
  sentences: string[];
  subaddress: string | null;
  // Joined from the call record and the engine's own report, for calls on either fax engine.
  engine?: 'builtin' | 'hylafax' | null;
  engine_label?: string | null;
  mode_label?: string | null;
  outcome?: string | null;
  changes?: string[];
}

// What failed with this number, per direction, with when Faxbot stops counting it.
export interface FaxMachineMemory {
  direction: 'outbound' | 'inbound';
  kind: 't38_failed' | 'audio_failed';
  learned_at: string;
  expires_at: string;
  active: boolean;
  ended: 'forgotten' | 'expired' | 'went_through' | null;
}

export interface FaxMachineView {
  number: string;
  sentence: string;
  calls: FaxMachineCall[];
  learned: {
    t38_now: boolean; max_rate: number | null; inbound_rate: number | null; sentences: string[];
    audio?: boolean; compression?: string | null; ecm_on?: boolean; notes?: string[]; since?: string | null;
  };
  memory?: FaxMachineMemory[];
  can_forget?: boolean;
  iaf: 'peer' | 'endpoint' | null;
}

export interface IafServer {
  id: string;
  number: string;
  kind: 'peer' | 'endpoint';
  label: string;
  added_by: string | null;
  added_at: string;
  removed_at: string | null;
}
