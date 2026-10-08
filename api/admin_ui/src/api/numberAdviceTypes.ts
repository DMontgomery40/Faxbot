// Number advice (routing/number_http.py): where each number should live, your NPI record, the check before a first
// fax, and US prices by where a call starts. Money is [{currency, amount}]; an empty list is unknown, never $0.
import type { Money } from './deliveryTypes';

export interface PortingSource { label: string | null; url: string; read_on: string | null; secondary: boolean }

export interface PortingSteps {
  from: string;
  to: string;
  steps: string[];
  fee: string;
  lead_time: string;
  restriction: string | null;
  sources: PortingSource[];
}

export interface NpiEvidence { state: 'listed' | 'not_listed'; read_at: string | null; sentence: string | null }

export interface PlacementCost {
  account: string;
  current: boolean;
  monthly: Money[];
  faxes: Money[];
  number_fee: Money[];
  reason: string | null;
}

export interface PlacedNumber {
  number: string;
  display: string;
  kind: string;
  account: string;
  received: number;
  pages: number;
  costs: PlacementCost[];
  skipped: string[];
  notes: string[];
  npi_record: NpiEvidence | null;
  cheapest: string | null;
  saving: Money[];
  porting: PortingSteps | null;
  state: 'move' | 'keep' | 'unknown';
  sentence: string;
}

export interface AccountWorth { account: string; monthly_fee: Money[]; saving: Money[]; sentence: string; porting: PortingSteps[] }

export interface NumberPlacement {
  days: number;
  estimate: boolean;
  state: 'advice' | 'keep' | 'nothing_to_move' | 'one_account' | 'unknown' | 'account' | 'no_numbers';
  sentence: string;
  numbers: PlacedNumber[];
  accounts: AccountWorth[];
  note: string;
  assumptions?: string[];
}

export interface NpiNumber { number: string; display: string; kind: 'fax' | 'phone'; where: string; address: string | null }

export interface NpiRecord {
  npis: Array<{ npi: string; label: string | null; name: string | null; read_at: string | null; numbers: NpiNumber[] }>;
  sentence: string;
  source_url: string;
  problem?: string;
}

export interface RecipientCheck {
  number: string;
  first_send: boolean;
  checked: boolean;
  state: string;
  warning: boolean;
  sentence: string | null;
  name: string | null;
  listed: Array<{ npi: string; name: string | null; kind: string; where: string; address: string | null; read_on: string | null }>;
  source_url: string;
}

export interface StatePrices {
  route: string;
  carrier: string;
  rows: number;
  differ: number;
  source_url: string | null;
  read_on: string | null;
  imported_at: string | null;
}

export interface SiteAdvice {
  days: number;
  estimate: boolean;
  sentence: string;
  carriers: Array<{ account: string; carrier: string; by_jurisdiction: boolean; sentence: string }>;
  items: Array<{ from_site: string; to_site: string; state: string; state_name: string; faxes: number; saving: Money[];
    sentence: string; action: string }>;
  prices: StatePrices[];
  caller_id: string;
}
