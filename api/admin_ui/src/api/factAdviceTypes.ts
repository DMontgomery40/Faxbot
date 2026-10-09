// Advice about missing facts, quiet lines and moving a number (routing/fact_advice.py, routing/number_placement.py,
// routing/number_moves.py). Money is always a list of {currency, amount}; an empty list is unknown, never zero.
import type { Money } from './deliveryTypes';

export interface FactRow {
  fact: string;
  kind: 'authorization' | 'information' | 'price';
  title: string;
  boundary: boolean;
  saving: Money[];
  net: Money[];
  faxes: number;
  establish_cost: Money[];
  establish: string;
  confirm: string;
  step: string;
  chance: number | null;
  break_even: Record<string, Money[]> | null;
  unknown: boolean;
  realized: boolean;
  sentence: string;
}

export interface FactRecipient {
  number: string;
  display: string;
  name: string | null;
  kind: 'recipient' | 'your_number';
  faxes: number;
  baseline: Money[];
  unpriced: number;
  sentence: string | null;
  facts: FactRow[];
  largest: Money[];
}

export interface FactAdvice {
  days: number;
  state: 'advice' | 'none' | 'nothing_sent';
  sentence: string;
  recipients: FactRecipient[];
  estimate: boolean;
  realized: string;
  note: string;
  catalogue: Array<{ fact: string; kind: string; title: string }>;
  assumptions: string[];
}

export interface LineEvidence {
  last_arrival: string | null;
  last_arrival_text: string;
  senders: number;
  window_days: number;
  covered_days: number;
  months: Array<{ month: string; label: string; arrivals: number }>;
  npi_record: { state: string; sentence: string | null } | null;
  quiet_bound: { per_day: number; days: number; sentence: string; limit: string } | null;
  removed: Money[];
  removed_sentence: string;
}

export interface Dependency {
  question: string;
  label: string;
  answer: 'yes' | 'no' | 'unknown';
  note: string | null;
  answered_by: string | null;
  answered_on: string | null;
}

export interface LineRow {
  number: string;
  display: string;
  account: string;
  verdict: 'keep' | 'move_termination' | 'investigate' | 'can_likely_go';
  verdict_label: string;
  sentence: string;
  reasons: string[];
  evidence: LineEvidence;
  dependencies: Dependency[];
  carrier_facts: Array<{ sentence: string; source: string; read_on: string }>;
  move: { state: string; sentence: string } | null;
}

export interface LineAdvice {
  state: string;
  sentence: string;
  numbers: LineRow[];
  questions: Array<{ question: string; label: string; help: string }>;
  note: string;
}

export interface MoveStep {
  step: string;
  stage: 'dependencies' | 'before' | 'after';
  label: string;
  state: 'done' | 'waiting' | 'blocked' | 'failed';
  state_label: string;
  evidence: string[];
  action: string | null;
  action_label: string | null;
}

export interface ReceiptTest {
  id: string;
  origin: string;
  origin_label: string;
  started_text: string;
  state: 'waiting' | 'arrived_new' | 'arrived_old' | 'arrived_both' | 'not_sent';
  sentence: string;
}

export interface MoveRecord {
  number: string;
  display: string;
  state: 'none' | 'open' | 'finished' | 'abandoned';
  sentence: string;
  from_account: string | null;
  to_account: string | null;
  started_text: string | null;
  steps: MoveStep[];
  tests: ReceiptTest[];
  arrivals: { old: number; new: number; both: number; sentence: string } | null;
  origins: Array<{ key: string; label: string }>;
  accounts: Array<{ key: string; label: string }>;
  note: string;
}
