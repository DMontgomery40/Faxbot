// Savings & optimization → Charges and Savings & optimization → Invoices (routing/charges_http.py).
import type { Money } from './deliveryTypes';

// How Faxbot reads one account's charges, and when it last listed the account's faxes at its provider.
export interface ChargeSource {
  account_key: string;
  provider_id: string;
  label: string;
  sentence: string;
  listing: boolean;
  last_checked: string | null;
  last_outcome: 'complete' | 'partial' | 'unavailable' | 'unsupported' | null;
}

export interface ReceivedCharges {
  provider_id: string;
  label: string;
  faxes: number;
  charged: number;
  waiting: number;
  never_priced: number;
  cost: Money[];
  summary: string | null;
}

// A fax a provider listed that Faxbot has no record of. Read only: Faxbot never acts on it.
export interface UnrecordedFax {
  id: string;
  account_key: string;
  provider_id: string;
  label: string;
  direction: 'sent' | 'received';
  from_number: string | null;
  to_number: string | null;
  time: string | null;
  pages: number | null;
  cost: Money | null;
  may_be_uncertain_fax: string | null;
  summary: string;
}

export interface TrunkRecords {
  preset: string | null;
  published: boolean;
  readable: boolean;
  sources: { title: string; url: string; read_on: string }[];
  sentence: string;
}

export interface ChargesView {
  since: string;
  accounts: ChargeSource[];
  received: ReceivedCharges[];
  unrecorded: UnrecordedFax[];
  trunk: TrunkRecords;
}

export interface SweepResult {
  account_key: string;
  provider_id: string;
  label: string;
  outcome: 'complete' | 'partial' | 'unavailable' | 'unsupported';
  listed: number;
  unrecorded: number;
  summary: string;
}

export interface SweepResponse {
  results: SweepResult[];
  summary: string;
}

export interface InvoicePart {
  label: string;
  amount: Money;
  kind: 'plan' | 'reported' | 'estimated' | 'unrecorded';
}

export interface Invoice {
  id: string;
  account_key: string;
  provider_id: string;
  label: string;
  first_day: string;
  last_day: string;
  period_name: string;
  total: Money;
  explained: Money;
  residual: Money;
  state: 'explained' | 'residual' | 'incomplete';
  complete: boolean;
  summary: string;
  parts: InvoicePart[];
  notes: string[];
  faxes: { sent: number; received: number; calls: number; not_priced: number };
  note: string | null;
  file: { name: string | null; type: string | null; size: number | null } | null;
  version: number;
  entered_by: string | null;
  entered_at: string;
}

export interface InvoiceDetail extends Invoice {
  current: boolean;
  history: { id: string; version: number; total: Money; entered_by: string | null; entered_at: string; note: string | null }[];
}

export interface InvoiceRecommendation {
  account_key: string;
  direction: 'more' | 'less';
  invoices: string[];
  text: string;
}

export interface InvoiceAccount {
  account_key: string;
  provider_id: string;
  label: string;
  currency: string;
  billing_day: number;
}

export interface InvoicesView {
  invoices: Invoice[];
  recommendations: InvoiceRecommendation[];
  accounts: InvoiceAccount[];
}

export interface InvoiceInput {
  account: string;
  total: string;
  currency: string;
  month?: string;
  firstDay?: string;
  lastDay?: string;
  note?: string;
  file?: File | null;
}
