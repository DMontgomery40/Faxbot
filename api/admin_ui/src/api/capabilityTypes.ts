// GET /routing/capabilities: every mechanism in the catalogue, grouped by the outcome it serves, for Savings &
// optimization → Capabilities, the Overview and `faxbot costs capabilities` (api/app/routing/capabilities.py).
// Every sentence comes from the server, and it never carries money. __tests__/capabilities.json is one answer.
import type { SavingsMechanism } from './deliveryTypes';

export type CapabilityFilterKey = 'on' | 'off' | 'ready' | 'needs' | 'experimental';
export type CapabilityPrerequisiteKind =
  'connection' | 'prices' | 'engine' | 'agreement' | 'partner' | 'setting' | 'permission' | 'history';
// How a capability that is ready to turn on is marked among the next improvements.
export type CapabilityImprovementKind = 'now' | 'fact' | 'agreement' | 'experimental';

export interface CapabilityPrerequisite {
  kind: CapabilityPrerequisiteKind;
  kind_label: string;
  met: boolean;
  // 'In place' or 'Missing'.
  label: string;
  // Who or what satisfies it.
  sentence: string;
  // The console address where it is satisfied ('recipients/partners'), and that page's name.
  address: string;
  address_label: string;
}

// A console address, its page's name, and the faxbot command that does the same.
export interface CapabilityLink {
  address: string;
  label: string;
  command: string;
}

export interface Capability {
  key: string;
  name: string;
  sentence: string;
  // A concrete use of it.
  example: string;
  outcome: string;
  // Its own page: 'savings/capabilities?key=<key>'.
  address: string;
  // The savings map's facts, unchanged.
  enabled: SavingsMechanism['enabled'];
  works: SavingsMechanism['works'];
  evidence: SavingsMechanism['evidence'];
  here: SavingsMechanism['here'];
  experimental: boolean;
  // Off, but works here.
  ready: boolean;
  // How many prerequisites are missing.
  missing: number;
  // The page's filters it matches, in the filters' order.
  filters: CapabilityFilterKey[];
  // Set only when it is ready to turn on.
  improvement: { kind: CapabilityImprovementKind; label: string } | null;
  prerequisites: CapabilityPrerequisite[];
  // Where its setting lives (for advice and charge checks: the page with what they found).
  setting: CapabilityLink;
  // Its figures: its part on Savings, or the page with its own advice or findings; null when it keeps none.
  results: CapabilityLink | null;
  // The faxes it acted on, where the console has a list of exactly those.
  affected: { address: string; label: string } | null;
}

export interface CapabilityOutcome {
  key: string;
  title: string;
  sentence: string;
  capabilities: Capability[];
}

export interface Capabilities {
  days: number;
  title: string;
  sentence: string;
  legend: Array<{ label: string; sentence: string }>;
  filters: Array<{ key: CapabilityFilterKey; label: string; sentence: string }>;
  outcomes: CapabilityOutcome[];
}
