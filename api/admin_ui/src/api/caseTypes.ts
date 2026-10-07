// Case packets, continued: what a recipient acknowledged, kept originals, repairs and checklists.

// Where a document stands for one recipient. Delivered is not acknowledged: only the
// recipient's acknowledgement lets a later packet list a document instead of sending it.
export type CaseDocumentState = 'waiting' | 'not_sent' | 'sent' | 'accepted' | 'expired' | 'invalidated';

// How the recipient's acknowledgement arrived.
export type CaseAcknowledgement = 'partner_receipt' | 'work_acknowledged' | 'received_fax' | 'person';

// Why a document goes in full in a packet ('accepted' when it is listed on the index instead).
export type CaseWhy = 'new' | 'sent' | 'waiting' | 'not_sent' | 'expired' | 'invalidated' | 'references_off' | 'accepted'
  | 'repair';

export interface CaseRecipient {
  to: string;
  reuse_days: number;
  reuse_days_default: number;
  reuse_days_set: boolean;
  version: number;
}

export interface CaseOriginal {
  id: string;
  title: string;
  pages: number;
  reference: string;
  document_type: string;
  document_date: string | null;
  source: string;
  version: string;
  added_at: string;
  added_by: string | null;
}

export interface CaseOriginalDraft {
  file: File;
  title: string;
  type?: string;
  date?: string;
  version?: string;
  source?: string;
}

export interface CaseRepair {
  case_id: string;
  to: string;
  pages: number;
  documents: Array<{ title: string; pages: number; status: 'included'; version?: string; source?: string }>;
  missing: string[];
  packets_in_flight: number;
  reason: string;
  fax_id: string | null;
}

export interface ChecklistItem {
  type: string;
  required: boolean;
  within_days: number | null;
  version: string | null;
}

export interface CaseChecklist {
  id: string;
  name: string;
  version: number;
  to: string | null;
  items: ChecklistItem[];
  created_at: string;
  created_by: string | null;
  used: number;
}

export interface CaseChecklists {
  checklists: CaseChecklist[];
  suggestions: boolean;
  example: { name: string; items: ChecklistItem[] };
}

export interface ChecklistPick {
  item: number | null;
  original_id: string;
  title: string;
  pages: number;
  document_type: string;
  document_date: string | null;
  version: string;
  source: string;
  reason: string;
}

export interface ChecklistBuild {
  case_id: string;
  to: string;
  as_of: string;
  purpose: string;
  checklist: { id: string; name: string; version: number };
  items: ChecklistItem[];
  selected: ChecklistPick[];
  missing: Array<{ item: number; type: string; required: boolean; reason: string }>;
  required_not_selected: Array<{ item: number; type: string }>;
  suggestions: Array<{ item: number; original_id: string; title: string; reason: string }>;
  suggestions_enabled: boolean;
  packet: { pages: number; pages_saved: number; documents: Array<{ title: string; pages: number; status: string; why?: CaseWhy }> } | null;
  fax_id: string | null;
}

export interface ChecklistBuildRequest {
  to: string;
  checklist_id: string;
  as_of?: string;
  purpose?: string;
  preview: boolean;
  selection?: Array<{ original_id: string; item: number | null }>;
  allow_missing?: boolean;
}
