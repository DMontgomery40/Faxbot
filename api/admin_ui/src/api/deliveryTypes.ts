import type { RuleSuggestion } from '../components/ProviderRulesSuggest';
import type { OriginRateRow } from '../components/ProviderAccountsTrunks';
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

// One route's faxes to one number over the last 30 days and what one delivered fax cost,
// counting every attempt on the route (failed and uncertain ones too).
export interface DeliveredCost {
  route: string;
  label: string;
  attempts: number;
  delivered: number;
  failed: number;
  uncertain: number;
  cancelled: number;
  delivered_percent: number | null;
  // Null for a flat plan, a direct partner, an attempt without a price, or no delivered fax.
  cost_per_delivered: Money | null;
  total_cost: Money | null;
  // True when any attempt's cost is Faxbot's estimate rather than a charge.
  estimate: boolean;
  charged_attempts: number;
  estimated_attempts: number;
  unpriced_attempts: number;
  included_in_plan: boolean;
  direct: boolean;
  average_pages: number | null;
  average_connected_seconds: number | null;
  // Enough delivered faxes for Faxbot to choose routes by this figure.
  enough_evidence: boolean;
  // The cell text, such as "$0.0089", "About $0.012" or "Included in your plan".
  cost_text: string;
  // Where the cost came from, such as "9 charged, 5 estimated".
  basis_text: string | null;
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
  // Each route's cost per delivered fax, the cheapest first (absent from a save response).
  delivered_costs?: DeliveredCost[];
  // Calls at once to this number: null is the default (one at a time), 0 means no limit.
  max_calls?: number | null;
}

// A number where another route would cost less than the one Faxbot uses first now:
// `plan` is a flat plan that already includes faxes (you chose a metered route),
// `cheaper_route` another metered route that cost less per delivered fax.
export interface SendingRecommendation {
  // A routing rule that would do the same for every number like this one; Add as rule puts it in the draft.
  rule_suggestion?: RuleSuggestion | null;
  number: string;
  display_name: string | null;
  version: number;
  preferred_route: string | null;
  chosen_by_you: boolean;
  kind: 'plan' | 'cheaper_route';
  current_label: string;
  // Null when the route used now has no faxes to this number in the last 30 days (a plan suggestion only).
  current: DeliveredCost | null;
  suggested: DeliveredCost;
  // Null for a plan: the plan's fee is already paid, so Faxbot claims no saving per fax.
  saving_per_fax: Money | null;
  sentence: string;
}

export interface SendingRecommendations {
  window_days: number;
  min_delivered: number;
  items: SendingRecommendation[];
  empty_sentence: string;
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

// What a fax would take and cost on one route before sending (GET /routing/predict); every figure an estimate.
export interface RoutePrediction {
  route: string;
  label: string;
  // Pages charged at a page price: 0 on a route that bills by time or includes faxes in a plan.
  billed_pages: number | null;
  // Predicted time on the line in seconds, and what the carrier bills for it (per-minute routes only).
  seconds: number | null;
  billed_seconds: number | null;
  seconds_to_next_step: number | null;
  // Null when unknown, never zero.
  cost: Money | null;
  cost_text: string | null;
  // A monthly plan: the cost is what this fax adds to the bill.
  marginal: boolean;
  headline: string;
  basis: string;
}

export interface PredictionAnswer {
  to: string;
  number_class: string;
  number_class_text: string;
  pages: number;
  layout: string;
  resolution: string;
  routes: RoutePrediction[];
  sentence: string;
  note: string;
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
  max_calls?: number | null;
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
  // Of attempts_without_reported_cost, those with no estimate either: in no total, never counted as $0.
  attempts_not_priced?: number;
  // Of attempts_with_reported_cost, those the carrier priced only in part by the give-up time: the priced part
  // is in reported_cost; the rest will never be priced.
  attempts_never_priced?: number;
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
  // Of calls_without_reported_cost, those with no estimate either (such as an answered call of unknown length).
  calls_not_priced?: number;
  // Of calls_with_reported_cost, those the carrier priced only in part by the give-up time.
  calls_never_priced?: number;
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
  // Sent faxes and received calls with no charge and no estimate: left out of total_cost.
  not_priced?: number;
  // Sent faxes and received calls the carrier never priced in full: only their priced parts are in total_cost.
  never_priced?: number;
}

// One fax's cost: what the carrier charged, or why it is not known yet.
export interface FaxCost {
  state: 'reported' | 'partial' | 'incomplete' | 'waiting' | 'unmatched' | 'included' | 'local' | 'none';
  summary: string | null;
  reported_cost: Money[];
  estimated_cost?: Money[];
  /** The route that carried the latest attempt ('direct', 'sip' or a provider id), and every route tried in order. */
  route?: string | null;
  routes?: string[];
  // Why the latest attempt went by its route, in one sentence, as recorded when Faxbot chose it.
  route_reason?: string | null;
  route_explanation?: string | null;
  // The number the fax dialed when it was not the one entered (the recipient's approved toll-free number),
  // or why it went back to the number entered; null when it simply called the number entered.
  dialed?: DialedNumber | null;
}

// GET /routing/rate-cards toll_free: what one sending route publishes about calling toll-free numbers.
export interface TollFreeTerms {
  provider_id: string;
  route: string;
  provider_name: string;
  label: string;
  reaches: 'yes' | 'no' | 'not_published';
  reach_text: string;
  price_text: string;
  pricing: 'own' | 'same_as_card' | 'not_published';
  caller_id_text: string | null;
  advertised_on: string | null;
  source_url: string | null;
}

export interface DialedNumber {
  number: string;
  display: string;
  toll_free: boolean;
  recipient_name: string | null;
  approved_on: string | null;
  withdrawn_on: string | null;
  sentence: string;
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
  // Prices by where calls start (several trunks and sites), when the card has them.
  rows?: OriginRateRow[];
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
  // The received fax this item delivers (the Inbox's fax id). A direct delivery has one when it was filed as a
  // received fax (every one since peer fax); an earlier direct delivery has none.
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
  // Where a delivered item was emailed, as the email server accepted it at the time.
  delivered_to?: string[];
  // False for an email delivered before Faxbot kept its recipients.
  recipients_recorded?: boolean;
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
  // Fax images (peer fax): whether this installation accepts them from the partner (on by default), and whether
  // the partner said, signed, that it accepts them from us; optional for older servers.
  receive_fax_images?: boolean;
  partner_receives_fax_images?: boolean;
  fax_images_text?: string | null;
}

// POST /direct/peers/{id}/fax-images: the partner as it now stands, the sentence to show and whether it was told now.
export type DirectFaxImagesResult = DirectPartner & { detail: string; partner_told: boolean };

// GET /direct/deliveries: recent direct deliveries. For a sent document the
// message id is the fax's delivery attempt id.
export interface DirectDeliveryRecord {
  message_id: string;
  direction: 'inbound' | 'outbound';
  partner: string | null;
  fax_number: string;
  state: 'sending' | 'accepted' | 'refused' | 'uncertain';
  // 'fax_image' when the exact fax image went directly (never "faxed"); optional for older servers.
  kind?: 'original' | 'fax_image';
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
  // Signed: a negative amount cost more than it saved; total_sentence says so in words.
  total_saved: Money[];
  total_sentence?: string;
  sending_together: SavingPart & {
    numbers: number; calls: number; faxes: number; calls_saved: number; priced_calls: number;
  };
  // Separator pages shared calls left out (an index page or page marks); optional for older servers.
  separator_pages?: SavingPart & { calls: number; pages_saved: number; priced_calls: number };
  direct_delivery: SavingPart & {
    faxes: number; calls_avoided: number; pages: number; priced: number; in_plan: number; unpriced: number;
  };
  case_packets: SavingPart & {
    counted_from: string | null; earlier_not_counted: boolean; counted_from_sentence: string | null;
    packets: number; documents_left_out: number; pages_not_resent: number; pages_saved: number;
    priced: number; in_plan: number; unpriced: number;
  };
  // Faxes whose pages went over SSL Fax (the fast fax service); optional for older servers.
  sslfax?: SavingPart & {
    faxes: number; seconds_saved: number; priced: number; in_plan: number; unpriced: number; same_cost: number;
  };
  // Telephone calls avoided by fax images partners accepted directly; optional for older servers.
  direct_fax_images?: SavingPart & {
    faxes: number; calls_avoided: number; pages: number; priced: number; in_plan: number; unpriced: number;
  };
  // Faxes to this installation's own numbers, delivered inside Faxbot with no call; optional for older servers.
  own_numbers?: SavingPart & {
    faxes: number; calls_avoided: number; pages: number; priced: number; in_plan: number; unpriced: number;
  };
  // Faxes that called their recipient's approved toll-free number; the recipient pays those calls.
  toll_free?: SavingPart & {
    faxes: number; priced: number; in_plan: number; unpriced: number;
  };
  // Pages saved by packing them onto long pages, and blank page bottoms left out; optional for older servers.
  packing?: SavingPart & {
    faxes: number; pages_saved: number; trimmed_pages: number; seconds_saved: number; priced: number;
    in_plan: number; plan_pages: number; unpriced: number;
  };
}

// GET /routing/recommendations/plans: whether each monthly plan is worth its fee. Every figure is an estimate;
// windows[0] is the last 30 days and windows[1] the 30 days before, each counted over the days Faxbot has records for.
export interface PlanWindow {
  start: string;
  end: string;
  days: number;
  sent: number;
  received: number;
  own_numbers: number;
  // Of own_numbers, tests to a number that does not receive into Faxbot: still a paid call, so priced.
  paid_tests?: number;
  fee: Money[];
  fee_per_fax: Money[];
  other_way: Money[];
  number_rental: Money[];
  // The carrier publishes no price for keeping the plan's number: unknown, not free.
  number_rental_unpublished?: boolean;
  other_routes: string[];
  without_other_way: number;
}

export interface PlanAdvice {
  route: string;
  name: string;
  monthly_fee: Money[];
  state: 'keep' | 'review' | 'too_little_history';
  sentence: string;
  action: string | null;
  caveats: string[];
  estimate: true;
  windows: PlanWindow[];
}

export interface PlanRecommendations {
  days: number;
  estimate: true;
  plans: PlanAdvice[];
  empty_sentence: string | null;
}

// GET /routing/recommendations/receiving: shared lines, numbers with few calls and fax services. Every figure is an
// estimate; "choose" is the earlier window the advice is picked from, "check" the later one it is judged on.
export interface ReceivingWindow { start: string; end: string; days: number }

export interface ReceivingCosts {
  billed_by_the_minute: Money[];
  channels: Money[];
  still_billed_by_the_minute: Money[];
  number_rental: Money[];
  total_today: Money[];
  total_with_pool: Money[];
  difference: Money[];
}

export interface ReceivingNumber {
  number: string;
  kind: 'local' | 'toll_free' | 'international' | 'other';
  eligible: boolean;
  reason: string | null;
  in_pool: boolean;
  calls_before: number;
  calls: number;
  billed_by_the_minute: Money[];
  // Calls with no price in either period: the cost is unknown, never $0.
  unpriced_calls?: number;
}

export interface ReceivingPool {
  state: 'share' | 'turned_away' | 'keep_metered' | 'not_saving' | 'too_little_history' | 'no_channel_price' | 'no_trunk'
    | 'unpriced';
  sentence: string;
  numbers: ReceivingNumber[];
  note?: string | null;
  // With state unpriced: what to enter where, and the numbers whose calls have no price.
  action?: string;
  unpriced_numbers?: string[];
  pool_numbers?: string[];
  channels?: number;
  calls?: number;
  turned_away?: number;
  peak?: number;
  needed?: number;
  busy_windows?: Array<{ start: string; end: string; turned_away: number; numbers: string[] }>;
  busy_windows_total?: number;
  check?: ReceivingCosts;
  choose?: ReceivingCosts & { calls: number; peak: number; turned_away: number };
  break_even?: string | null;
  assumptions?: string[];
}

export interface ReceivingRecommendations {
  days: number;
  estimate: true;
  carrier: string | null;
  sentence: string;
  windows: { choose: ReceivingWindow; check: ReceivingWindow };
  history: { enough: boolean; first_call_at: string | null; days: number };
  pool: ReceivingPool;
  quiet_numbers: {
    state: 'quiet' | 'none_quiet' | 'too_little_history' | 'no_trunk';
    sentence: string;
    // question: "Is … still printed …?", asked before giving any number up.
    numbers: Array<{ number: string; received: number; sent: number; monthly_rental: Money[]; question?: string | null }>;
    monthly_total: Money[];
  };
  connections: { sentence: string; items: Array<{ name: string; kind: 'trunk' | 'provider'; monthly_fee: Money[] }> };
  prices: Array<{ label: string; text: string; source_url: string | null; read_on: string | null }>;
  // HumbleFax's and eFax's own numbers, with their faxes in the window and their plan fee (never number rental).
  provider_numbers?: ProviderNumbers;
}

export interface ProviderNumber {
  provider: string;
  name: string;
  number: string;
  received: number;
  sent: number;
  enough_history: boolean;
  quiet: boolean;
  plan_fee: Money[];
  question: string | null;
  sentence: string;
}

export interface ProviderNumbers {
  state: 'none' | 'quiet' | 'none_quiet' | 'too_little_history';
  sentence: string;
  numbers: ProviderNumber[];
  most_faxes: number;
}

// GET /routing/recommendations/fax-marker: calls marked as fax against calls not marked, from history.
export interface FaxMarkerSide {
  calls: number;
  delivered: number;
  failed: number;
  result_unknown: number;
  delivered_percent: number | null;
  t38: number;
  audio: number;
  mode_unknown: number;
  t38_percent: number | null;
  average_seconds: number | null;
  seconds_per_page: number | null;
  cost_per_delivered: Money | null;
  cost_text: string;
  settled: number;
  reported: number;
  estimated: number;
  unpriced: number;
}

export interface FaxMarkerAdvice {
  days: number;
  min_calls: number;
  state: 'no_calls' | 'one_side' | 'too_few' | 'compared';
  enough: boolean;
  sentence: string;
  setting_on: boolean;
  setting_sentence: string;
  difference: string | null;
  caveat: string | null;
  left_out: number;
  left_out_sentence: string | null;
  marked: FaxMarkerSide;
  not_marked: FaxMarkerSide;
}

// GET /routing/recommendations/billing-steps: calls that end just past a billed step, per number.
export interface BillingStepNumber {
  number: string;
  display_name: string | null;
  calls: number;
  calls_near: number;
  seconds_past: { least: number; most: number };
  saving: Money;
  sentence: string;
}

export interface BillingSteps {
  days: number;
  estimate: true;
  carrier: string | null;
  state: 'no_trunk' | 'no_price' | 'not_by_time' | 'fine_steps' | 'too_few' | 'none_near' | 'near';
  sentence: string;
  min_calls: number;
  step: { seconds: number; minimum_seconds: number; near_seconds: number; price: Money; price_text: string;
    source_url: string | null; read_on: string } | null;
  numbers: BillingStepNumber[];
  numbers_total: number;
  calls_near: number;
  saving: Money | null;
}

// GET /routing/recommendations/partners: numbers whose faxes cost the most again and again.
export interface PartnerCandidate {
  number: string;
  display_name: string | null;
  faxes: number;
  delivered: number;
  average_pages: number | null;
  monthly_cost: Money | null;
  cost_text: string;
  estimate: true;
  unpriced_faxes: number;
  sentence: string;
  link: string;
  link_label: string;
}

export interface PartnerCandidates {
  days: number;
  min_faxes: number;
  estimate: true;
  state: 'candidates' | 'too_few' | 'none';
  sentence: string;
  items: PartnerCandidate[];
  items_total: number;
  link: string;
}

// A recipient's toll-free fax number: append-only rows, the newest first.
export interface TollFreeRow {
  id: string;
  number: string;
  alternate_number: string;
  alternate_display: string;
  action: 'noted' | 'approved' | 'withdrawn';
  approved_by: string | null;
  approved_on: string | null;
  evidence: string | null;
  recorded_by_name: string | null;
  recorded_at: string;
}

export interface TollFreeState {
  number: string;
  current: TollFreeRow | null;
  history: TollFreeRow[];
  approved_alternate: string | null;
  sentence: string | null;
}

export interface TollFreeChange {
  action: 'noted' | 'approved' | 'withdrawn';
  alternate_number?: string;
  approved_by?: string;
  approved_on?: string;
  evidence?: string;
}

export interface TollFreeSuggestion {
  source: string;
  npi: string;
  name: string | null;
  address_purpose: string;
  address: string | null;
  fax_number: string;
  fax_display: string;
  evidence: string;
  source_url?: string;
}

export interface TollFreeSuggestions {
  number: string;
  items: TollFreeSuggestion[];
  sentence: string;
}

export interface TollFreeRecommendations {
  state: 'none' | 'on_file';
  sentence: string;
  days: number;
  items: Array<TollFreeRow & { display_name: string | null; approved: boolean; spend: Money | null; sentence: string }>;
}
