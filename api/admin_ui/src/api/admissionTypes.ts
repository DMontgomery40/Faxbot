// Line windows and preflight between partners that take part (api/app/direct/admission.py, preflight.py).

export interface AdmissionSettings {
  lines: number | null;
  reserve: number;
  window_minutes: number;
  urgent: 'go' | 'wait';
  deadline: 'go' | 'wait';
}

export interface AdmissionGrant {
  direction: 'issued' | 'received';
  outcome: 'granted' | 'not_taking_part' | 'no_answer';
  earliest: string | null;
  latest: string | null;
  duration_class: 'short' | 'medium' | 'long';
  created_at: string;
  job_id: string | null;
}

export interface PreflightAnswer {
  direction: 'asked' | 'answered';
  document_class: string;
  purpose: string;
  mailbox: string | null;
  accepts: number | null;
  reason: string | null;
  expires_at: string | null;
  created_at: string;
  job_id: string | null;
}

export interface AdmissionPartner {
  peer_id: string;
  organization: string;
  take_part: boolean;
  refused_classes: string[];
  refusal_reason: string | null;
  version: number;
  grants: AdmissionGrant[];
  preflights: PreflightAnswer[];
  advice: string;
}

export interface PartnerAdmission {
  settings: AdmissionSettings;
  partner_lines: number;
  lines_sentence: string;
  policy_sentence: string;
  legacy: { calls: number | null; sentence: string };
  partners: AdmissionPartner[];
  not_a_receipt: string;
}
