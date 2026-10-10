// The California court profile (api/app/court): where it applies, the rules and courts it rests on, a court fax.

export interface CourtAttachment {
  kind: 'mailbox' | 'rule';
  target: string;
  name: string;
  sentence: string;
  attached_at: string | null;
  by: string | null;
}

export interface CourtSource { rule: string; url: string; read_on: string; says: string }

export interface CourtListing {
  county: string;
  status: 'accepts' | 'stopped' | 'does_not_accept';
  note: string;
  fax: string | null;
  cutoff: string | null;
  source: string;
  source_date: string | null;
  read_on: string;
}

export interface CourtProfile {
  attached: CourtAttachment[];
  summary: string;
  rules: CourtSource[];
  holidays: { source: { url: string; read_on: string; says: string }; years: number[];
    days: Array<{ date: string; name: string }> };
  courts: CourtListing[];
  not_established: string[];
  trend: string;
  mailboxes: Array<{ id: string; label: string }>;
  routing_rules: Array<{ id: string; name: string }>;
}

export interface CourtFax {
  job_id: string;
  purpose: 'file' | 'serve';
  purpose_text: string;
  case: { county: string; case_number: string | null; title: string;
    documents: Array<{ title: string; pages: number }>; served_party: string | null };
  source: 'sender' | 'mailbox' | 'rule';
  sentences: string[];
  timing: string[];
  court_note: string | null;
  record_ready: boolean;
}

// What Send a fax collects for a court fax; sent as POST /fax's `court` JSON.
export interface CourtDetails {
  purpose: 'file' | 'serve';
  county: string;
  case_number: string;
  plaintiff: string;
  defendant: string;
  documents: Array<{ title: string; pages: number }>;
  sender: string[];
  telephone: string;
  attorney_for: string;
  filing_fee: boolean;
  fee_account: string;
  by_fax_confirmed: boolean;
  agreement_confirmed: boolean;
  number_confirmed: boolean;
  served_party: string;
  fax: string;
}
