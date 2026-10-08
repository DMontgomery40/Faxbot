// Direct messages and FHIR (/digital/*): the HISP account and FHIR clients, each recipient's addresses, and the
// messages sent and received.

export type DigitalKind = 'hisp' | 'fhir';

export interface DigitalField {
  name: string;
  label: string;
  kind: 'text' | 'int' | 'bool' | 'pem' | 'choice' | 'money' | 'date' | 'lines';
  secret: boolean;
  required: boolean;
  default: string | number | boolean | null;
  choices: string[];
  help: string | null;
}

export interface DigitalAccountKind {
  id: DigitalKind;
  label: string;
  fields: DigitalField[];
}

export interface DigitalAccount {
  key: string;
  provider: DigitalKind;
  label: string;
  enabled: boolean;
  settings: Record<string, string | number | boolean | null>;
  secrets_set: string[];
  missing: string[];
  health: { state: 'ready' | 'not_set_up' | 'off'; sentence: string };
  plan: string | null;
  certificate?: { sentence: string; fingerprint: string | null } | null;
  trust_bundle?: { anchors: number; loaded_at: string; source_url: string | null; loaded_by: string | null } | null;
  public_keys?: { keys: Array<Record<string, string | string[]>> } | null;
  // Where the recipient's system reads the public key set (this Faxbot's public address); null until it is set.
  public_keys_url?: string | null;
}

export interface DigitalPlanPreset {
  id: string;
  label: string;
  kind: DigitalKind;
  currency: string;
  monthly_fee: string | null;
  price_per_message: string | null;
  included_messages: number | null;
  price_source: string;
  price_date: string;
  note: string;
}

export interface DigitalAccountsState {
  generation: number;
  accounts: DigitalAccount[];
  kinds: DigitalAccountKind[];
  presets: DigitalPlanPreset[];
}

export interface DigitalAccountInput {
  key: string;
  provider: DigitalKind;
  label?: string | null;
  settings: Record<string, string | number | boolean | null>;
  credentials: Record<string, string>;
}

export interface DigitalAccountPatch {
  label?: string;
  enabled?: boolean;
  settings?: Record<string, string | number | boolean | null>;
  credentials?: Record<string, string>;
}

export type DigitalAddressState = 'suggested' | 'confirmed' | 'withdrawn' | 'dismissed';

export interface DigitalAddress {
  id: string;
  kind: 'direct' | 'fhir';
  kind_label: string;
  address: string;
  organization: string | null;
  account_key: string | null;
  source: 'entered' | 'nppes';
  npi: string | null;
  evidence: string | null;
  state: DigitalAddressState;
  sentence: string;
  label: string;
  route_key: string;
  created_at: string;
  history: Array<{ action: DigitalAddressState; note: string | null; recorded_by_name: string | null;
    recorded_at: string }>;
}

export interface DigitalRecipient {
  number: string;
  addresses: DigitalAddress[];
  accounts: Array<{ key: string; label: string; kind: DigitalKind }>;
  sentence: string;
  nppes_sentence?: string;
}

export interface DigitalAddressInput {
  kind: 'direct' | 'fhir';
  address: string;
  account_key?: string | null;
  organization?: string | null;
  confirm?: boolean;
  note?: string | null;
}

export interface DigitalMessage {
  id: string;
  direction: 'out' | 'in';
  kind: 'direct' | 'fhir';
  account_key: string;
  job_id: string | null;
  counterpart: string;
  state: string;
  sentence: string | null;
  label: string;
  pages: number | null;
  created_at: string;
  updated_at: string;
  settled_at: string | null;
  events?: Array<{ kind: string; at: string; details: Record<string, unknown> }>;
}
