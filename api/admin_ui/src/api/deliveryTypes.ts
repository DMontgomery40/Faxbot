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
