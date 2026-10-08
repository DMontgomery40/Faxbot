// Sent faxes whose outcome Faxbot could not confirm (/certainty/*): an owner, checks ranked by cost, and settling.

export type CertaintyCheckKind = 'partner' | 'call_record' | 'receipt_query' | 'phone_call';

export interface CertaintyCheck {
  kind: CertaintyCheckKind;
  title: string;
  cost: string;
  automatic: boolean;
  // delivered, not_delivered or partial (a signed answer); probably_not_delivered, probably_partial or consistent
  // (a reading); asking, unavailable, unknown, not_done or sent.
  result: string;
  strength: 'proof' | 'reading' | null;
  text: string;
  meaning: string | null;
  action: 'send_query' | 'call' | null;
  script?: string[];
  fax_id?: string;
}

export type CertaintyOutcome = 'delivered' | 'not_delivered' | 'unknown';

export interface CertaintyItem {
  id: string;
  fax_id: string;
  reference: string;
  state: 'open' | 'settled';
  state_key: 'waiting' | 'assigned' | 'overdue' | 'settled';
  state_text: string;
  why: string;
  category: string;
  to_number: string;
  pages: number | null;
  sent_at: string | null;
  mailbox: string | null;
  owner: { id: string; name: string | null } | null;
  owner_source_text: string | null;
  due_at: string | null;
  due_hours: number | null;
  escalated_at: string | null;
  overdue: boolean;
  outcome: CertaintyOutcome | null;
  settled_by: string | null;
  settled_at: string | null;
  settled_reason: string | null;
  resend_fax_id: string | null;
  query_fax_id: string | null;
  is_mine: boolean;
  version: number;
  actions: Array<'assign' | 'settle' | 'send_query'>;
  // Detail only: the checks, cheapest first, what they point to, and the full number for people who may act.
  checks?: CertaintyCheck[];
  suggestion?: CertaintyOutcome | null;
  number?: string;
}

export interface CertaintyForFax {
  items: CertaintyItem[];
  // A fax sent again from an item, or a receipt query, points back to the fax it is about.
  about: { fax_id: string; kind: 'resend' | 'receipt_query' } | null;
}

export interface CertaintyCounts {
  open: number;
  mine: number;
  unassigned: number;
  overdue: number;
  settled: number;
}

export interface CertaintyEvent {
  kind: string;
  at: string;
  actor: string | null;
  text: string;
}

export interface CertaintyPerson {
  id: string;
  name: string;
  login: string | null;
}

export interface CertaintySettings {
  settle_hours: number;
  version: number;
  fallback: { id: string; name: string | null } | null;
  people: CertaintyPerson[];
}
