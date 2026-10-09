// Expected faxes (Faxes → Expected): faxes recorded before they arrive, their proposed matches,
// imports of open work from another system, and outage recovery. Times are naive UTC strings.

export type ExpectedView = 'waiting' | 'overdue' | 'proposed' | 'missing' | 'conflicts' | 'closed' | 'all';
export type ExpectedStateKey = 'waiting' | 'proposed' | 'overdue' | 'matched' | 'cancelled' | 'completed_elsewhere'
  | 'replaced';
export type ExpectedAction = 'cancel' | 'completed_elsewhere' | 'match' | 'resolve_conflict' | 'export';

export interface ExpectedFaxSummary {
  inbound_fax_id: string;
  from_number: string | null;
  to_number: string | null;
  pages: number | null;
  received_at: string | null;
  mailbox: string | null;
}

export interface ExpectedProposal {
  id: string;
  signal: string;
  strength: 'strong' | 'weak';
  text: string;
  version: number;
  // Null when you may not open the fax.
  fax: ExpectedFaxSummary | null;
  can_decide: boolean;
}

export interface ExpectedFax {
  id: string;
  code: string;
  reference: string;
  kind: string;
  description: string | null;
  required_parts: string[];
  required_revision: string | null;
  counterparty: string | null;
  fax_numbers: string[];
  direct_address: string | null;
  mailbox: string | null;
  mailbox_id: string;
  owner: { id: string; name: string | null } | null;
  state: string;
  state_key: ExpectedStateKey;
  state_text: string;
  due_at: string | null;
  due_text: string;
  overdue: boolean;
  source: string | null;
  operation_id: string | null;
  revision: string | null;
  replaces: string | null;
  missing_from_export: boolean;
  conflict: boolean;
  matched_at: string | null;
  match: { signal: string; by: string | null; fax: ExpectedFaxSummary | null } | null;
  also_arrived: number;
  proposals: ExpectedProposal[];
  keys: { subaddress: string | null; email_subject: string | null; message_id: string | null; form_field: string | null };
  created_at: string;
  version: number;
  actions: ExpectedAction[];
}

export interface ExpectedCounts {
  waiting: number;
  overdue: number;
  proposed: number;
  missing: number;
  conflicts: number;
  matched: number;
}

export interface ExpectedEvent {
  kind: string;
  occurred_at: string;
  actor: string | null;
  source: string;
  text: string;
}

export interface ExpectInput {
  reference: string;
  kind: string;
  mailbox_id: string;
  counterparty?: string;
  fax_numbers?: string[];
  description?: string;
  required_parts?: string[];
  required_revision?: string;
  due_at?: string;
  due_hours?: number;
  subaddress?: string;
  email_subject?: string;
  message_id?: string;
  direct_address?: string;
}

export interface ExpectedReport {
  days: number;
  expected: number;
  matched: number;
  matched_automatically: number;
  waiting: number;
  overdue: number;
  closed_otherwise: number;
  arrived: number;
  arrived_unmatched: number;
  not_stored: number;
  unmatched_expected: ExpectedFax[];
  unmatched_arrivals: Array<{ inbound_fax_id: string; from_number: string | null; received_at: string;
    mailbox: string | null; why: string }>;
  summary: string;
}

export const IMPORT_FIELDS = [
  'reference', 'operation_id', 'revision', 'kind', 'description', 'counterparty', 'fax_numbers', 'direct_address',
  'due', 'mailbox', 'subaddress', 'email_subject', 'message_id', 'required_parts', 'required_revision',
  'window_start',
] as const;
export type ImportField = typeof IMPORT_FIELDS[number];

export interface ImportSource {
  id: string;
  name: string;
  format: 'csv' | 'json';
  mapping: Partial<Record<ImportField, string>>;
  mailbox_id: string | null;
  mailbox: string | null;
  due_hours: number | null;
  subject_template: string | null;
  subaddress_template: string | null;
  form_field: string | null;
  revision_field: string | null;
  version: number;
}

export interface ImportSourceInput {
  id?: string;
  version?: number;
  name: string;
  format: 'csv' | 'json';
  mapping: Partial<Record<ImportField, string>>;
  mailbox_id?: string | null;
  due_hours?: number | null;
  subject_template?: string | null;
  subaddress_template?: string | null;
  form_field?: string | null;
  revision_field?: string | null;
}

export interface ImportRun {
  id: string;
  source: string | null;
  file_name: string | null;
  full_export: boolean;
  rows_total: number;
  created: number;
  unchanged: number;
  revised: number;
  conflicts: number;
  problems: Array<{ row: number; problem: string }>;
  problem_count: number;
  missing: Array<{ code: string; reference: string; mailbox: string | null }>;
  note: string | null;
  summary: string;
  created_at: string;
  completed_at: string | null;
  imported_by: string | null;
  replay?: boolean;
  reconciled_outage?: string | null;
}

export interface OutageAction {
  operation_id: string;
  revision: string | null;
  reference: string | null;
  action: string;
  channel: 'fax' | 'email' | 'phone' | 'other';
  outcome: 'done' | 'uncertain';
  fax_job_id: string | null;
  evidence_note: string | null;
  occurred_at: string;
  recorded_by: string | null;
}

export interface ReconciliationEntry {
  operation_id: string;
  revision: string | null;
  reference: string | null;
  mailbox: string | null;
  code: string | null;
  reason: string;
  text: string;
  actions?: OutageAction[];
}

export interface Reconciliation {
  id: string;
  created_at: string;
  created_by: string | null;
  already_done: ReconciliationEntry[];
  new: ReconciliationEntry[];
  unresolved: ReconciliationEntry[];
  summary: string;
}

export interface Outage {
  id: string;
  code: string;
  source: string | null;
  source_id: string;
  started_at: string;
  ended_at: string | null;
  note: string | null;
  declared_by: string | null;
  ended_by: string | null;
  open: boolean;
  version: number;
  text: string;
  actions?: OutageAction[];
  reconciliation?: Reconciliation | null;
}

export interface OutageActionInput {
  operation_id: string;
  revision?: string;
  reference?: string;
  action: string;
  channel: OutageAction['channel'];
  outcome: OutageAction['outcome'];
  fax_job_id?: string;
  evidence_note?: string;
}
