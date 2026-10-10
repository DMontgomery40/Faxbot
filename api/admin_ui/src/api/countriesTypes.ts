// Prices by the caller ID a call shows (Costs → Prices & plans): carrier decks and the caller IDs you confirmed.

export type OriginationType = 'local' | 'eea' | 'non_surcharged' | 'surcharged';

export interface CallerIdDeck {
  route: string;
  format: 'twilio' | 'faxbot';
  source_url: string | null;
  published_on: string | null;
  imported_at: string | null;
  imported_by: string | null;
  currency: string;
  rows: number;
  by_type: Partial<Record<OriginationType, number>>;
}

export interface CallerIdEligibility {
  account: string;
  caller_id: string;
  state: 'confirmed' | 'withdrawn';
  bought_here: boolean;
  evidence: string | null;
  evidence_url: string | null;
  recorded_by: string | null;
  recorded_at: string | null;
}

export interface SendingCaller {
  account: string;
  label: string;
  provider: string;
  provider_label: string;
  site: string | null;
  caller_id: string | null;
  priced_by_caller_id: boolean;
  sentence: string;
  eligibility: CallerIdEligibility | null;
}

export interface CallerIdPrices {
  decks: CallerIdDeck[];
  callers: SendingCaller[];
  layouts: { faxbot: string; telnyx: string };
}

export interface CallerIdRow {
  destination_prefix: string;
  origination_type: OriginationType;
  origination_label: string;
  origin_prefixes: string[];
  currency: string;
  per_minute: string;
  billing_increment_seconds: number;
  minimum_seconds: number;
  description: string | null;
}

export interface CallerIdQuote {
  account: string;
  label: string;
  eligibility: 'confirmed' | 'unconfirmed' | 'not_needed' | 'no_caller_id';
  caller_id: string | null;
  sentence: string;
  row: CallerIdRow | null;
  cheaper: CallerIdRow | null;
}

export interface CallerIdDeckImport {
  deck: CallerIdDeck;
  skipped: string[];
  skipped_count: number;
  terms: string;
}
