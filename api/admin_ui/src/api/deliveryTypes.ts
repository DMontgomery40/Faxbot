// Delivery routes, intake and direct delivery responses.

export interface Money {
  currency: string;
  amount: string;
}

export interface RouteEvidence {
  route: string;
  label: string;
  attempts: number;
  successes: number;
  failures: number;
  uncertain: number;
  success_percent: number | null;
  estimated_cost_30_days: Money[];
  reported_cost_30_days: Money[];
  last_attempt_at: string | null;
}

export interface Destination {
  number: string;
  display_name: string | null;
  notes: string | null;
  preferred_route: string | null;
  accepts_references: boolean;
  version: number;
  routes: RouteEvidence[];
  estimated_cost_30_days: Money[];
}

export interface RecommendedRoute {
  route: string;
  label: string;
  reason: string;
  explanation: string;
  estimated_cost_one_page: Money | null;
  // The estimate for a fax of `pages` pages (setup plus typical time a page, rounded as the card bills).
  pages?: number;
  estimated_cost?: Money | null;
  // The card's price in its own units, such as "$0.005 a minute, at least 1 minute" or "$0.07 a page".
  rate?: string | null;
  // A flat monthly plan: faxes are included and nothing is charged per fax.
  included_in_plan?: boolean;
  monthly_fee?: Money | null;
}

export interface DestinationDetail extends Destination {
  direct_partner: { organization: string; verified: boolean } | null;
  recommended_routes: RecommendedRoute[];
  available_routes: Array<{ route: string; label: string }>;
}

export interface DestinationPatch {
  display_name?: string | null;
  notes?: string | null;
  preferred_route?: string | null;
  accepts_references?: boolean;
  version?: number;
}

export interface ProviderCosts {
  provider_id: string;
  label: string;
  attempts: number;
  successes: number;
  failures: number;
  uncertain: number;
  billed_minutes: number;
  billed_pages: number;
  estimated_cost: Money[];
  reported_cost: Money[];
  settled_cost: Money[];
  attempts_without_reported_cost: number;
  // The carrier that reported these charges, such as Telnyx; null when none did.
  carrier?: string | null;
  attempts_with_reported_cost?: number;
  // Rate-card estimates for faxes the carrier has not reported yet (never double-counted).
  estimated_cost_not_reported?: Money[];
  awaiting_carrier_bill?: number;
  unmatched_charges?: number;
  plan?: { label: string; monthly_fee: Money; monthly_fee_text?: string; period_fee?: Money; period_days?: number } | null;
  // False when the provider has no rate card and nothing was reported: no published price.
  priced?: boolean;
  // Carrier records Faxbot has no call record of: already inside the charged total, counted here too.
  unrecorded_calls?: number;
  unrecorded_cost?: Money[];
  unrecorded_matched_to_faxes?: number;
  unrecorded_unmatched_cost?: Money[];
  // Charges, estimates for faxes not billed yet, and any plan fee for the period.
  total_cost?: Money[];
}

export interface ReceivedCosts {
  provider_id: string;
  label: string;
  carrier: string | null;
  calls: number;
  faxes: number;
  billed_minutes: number;
  estimated_cost: Money[];
  reported_cost: Money[];
  calls_with_reported_cost: number;
  calls_without_reported_cost: number;
  estimated_cost_not_reported: Money[];
  awaiting_carrier_bill: number;
  unmatched_charges: number;
  // Carrier records Faxbot has no call record of: already inside the charged total, counted here too.
  unrecorded_calls?: number;
  unrecorded_cost?: Money[];
  unrecorded_matched_to_faxes?: number;
  unrecorded_unmatched_cost?: Money[];
  // Charges, estimates for faxes not billed yet, and any plan fee for the period.
  total_cost?: Money[];
}

export interface CarrierChargeStatus {
  carrier: string | null;
  supported: boolean;
  readable: boolean;
}

export interface RouteCostsResponse {
  since: string;
  providers: ProviderCosts[];
  received?: ReceivedCosts[];
  carrier_charges?: CarrierChargeStatus;
  total_cost?: Money[];
}

// One fax's cost: what the carrier charged, or why it is not known yet.
export interface FaxCost {
  state: 'reported' | 'partial' | 'waiting' | 'unmatched' | 'included' | 'none';
  summary: string | null;
  reported_cost: Money[];
  estimated_cost?: Money[];
}

export interface ReconcileResult {
  checked: number;
  matched: number;
  charges_recorded: number;
  waiting: number;
  ambiguous: number;
  carrier_unavailable: boolean;
  summary: string;
}

// GET /routing/published-plans: what a provider publishes where its API has no published price.
export interface PublishedPlans {
  provider_id: string;
  country: string;
  sentence: string;
  // A card a person can save as their own estimate; null when no price could be read.
  card: RateCard | null;
  page_url: string | null;
  page_label: string;
}

export interface RateCard {
  id?: string | null;
  provider_id: string;
  label: string;
  direction: 'outbound' | 'inbound';
  currency: string;
  per_minute: string;
  per_page: string;
  per_call: string;
  billing_increment_seconds: number;
  minimum_seconds: number;
  source_url: string | null;
  captured_on: string;
  // A flat monthly plan fee; with no per-minute, per-page or per-call price, faxes are included.
  monthly_fee?: string | null;
  included_in_plan?: boolean;
}

export interface IntakeItem {
  id: string;
  source: 'fax' | 'direct';
  // The received fax this item delivers (the Inbox's fax id); null for a direct delivery.
  inbound_fax_id?: string | null;
  received_at: string;
  pages: number | null;
  from_number: string | null;
  to_number: string | null;
  state: 'received' | 'sending' | 'delivered' | 'failed';
  status: string;
  needs_action: boolean;
  attempts: number;
  next_attempt_at: string | null;
  delivered_at: string | null;
  connector: string | null;
  // Where a delivered item was emailed.
  delivered_to?: string[];
}

export interface IntakeCounts {
  received: number;
  sending: number;
  delivered: number;
  failed: number;
}

export interface EmailConnector {
  id: string;
  kind: 'email';
  name: string;
  enabled: boolean;
  match_number: string | null;
  host: string;
  port: number;
  security: 'starttls' | 'tls' | 'none';
  username: string;
  has_password: boolean;
  from_address: string;
  recipients: string[];
  subject_template: string;
  managed: boolean;
  version: number;
}

export interface EmailConnectorInput {
  name: string;
  enabled: boolean;
  match_number: string | null;
  host: string;
  port: number;
  security: 'starttls' | 'tls' | 'none';
  username: string;
  password: string | null;
  from_address: string;
  recipients: string[];
  subject_template: string;
  version?: number;
}

export interface DirectCard {
  faxbot_direct: number;
  organization: string;
  fax_number: string;
  endpoint: string;
  signing_key: string;
  exchange_key: string;
  signature: string;
}

export interface DirectPartner {
  id: string;
  organization: string;
  fax_number: string;
  endpoint: string;
  state: 'pending' | 'verified' | 'revoked';
  status: string;
  code_sent: boolean;
  code_expires_at: string | null;
  verified_at: string | null;
  expires_at: string | null;
  version: number;
}

// GET /direct/deliveries: recent direct deliveries. For a sent document the
// message id is the fax's delivery attempt id.
export interface DirectDeliveryRecord {
  message_id: string;
  direction: 'inbound' | 'outbound';
  partner: string | null;
  fax_number: string;
  state: 'sending' | 'accepted' | 'refused' | 'uncertain';
  status: string;
  size_bytes: number;
  created_at: string;
  accepted_at: string | null;
}

// GET /cases/{case}/documents?to=: documents of a case already sent to one recipient.
export interface CaseDocument {
  title: string;
  pages: number;
  reference: string;
  accepted: boolean;
  accepted_at: string | null;
  fax_id: string | null;
}

export interface CaseDocuments {
  case_id: string;
  to: string;
  accepts_references: boolean;
  documents: CaseDocument[];
}

// POST /cases/{case}/faxes: the packet, sent or previewed (fax_id is null for a preview).
export interface CasePacket {
  case_id: string;
  to: string;
  accepts_references: boolean;
  pages: number;
  pages_saved: number;
  documents: Array<{ title: string; pages: number; status: 'included' | 'referenced' }>;
  fax_id: string | null;
}

// GET /cases: the newest cases this installation sent packets for, one row per recipient.
export interface CaseSummary {
  case_id: string;
  to: string;
  documents: number;
  // Documents the recipient has (the fax that carried them finished).
  accepted: number;
  pages: number;
  last_sent_at: string | null;
  accepts_references: boolean;
}

// GET /routing/savings: what sending together, direct delivery and case packets saved. Always estimates.
interface SavingPart {
  estimate: true;
  saved: Money[];
  sentence: string;
}

export interface Savings {
  days: number;
  since: string;
  estimate: true;
  sentence: string;
  total_saved: Money[];
  sending_together: SavingPart & {
    numbers: number; calls: number; faxes: number; calls_saved: number; priced_calls: number;
  };
  direct_delivery: SavingPart & {
    faxes: number; calls_avoided: number; pages: number; priced: number; in_plan: number; unpriced: number;
  };
  case_packets: SavingPart & {
    counted_from: string | null; earlier_not_counted: boolean; counted_from_sentence: string | null;
    packets: number; documents_left_out: number; pages_not_resent: number; pages_saved: number;
    priced: number; in_plan: number; unpriced: number;
  };
}
