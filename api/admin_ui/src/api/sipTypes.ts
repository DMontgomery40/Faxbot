// SIP trunk settings, status and call records served under /admin/sip.

export type SipAuthMode = 'registration' | 'ip';

export interface SipPresetSource {
  url: string;
  read_on: string;
}

export interface SipPreset {
  id: string;
  label: string;
  host: string;
  port: number;
  transport: 'udp' | 'tcp' | 'tls';
  auth_modes: SipAuthMode[];
  codecs: string[];
  needs_host: boolean;
  ip_dial_prefix: boolean;
  t38: string;
  notes: string[];
  sources: SipPresetSource[];
}

export interface SipTrunkSettings {
  preset: string;
  auth: SipAuthMode;
  host: string;
  port: number;
  transport: string;
  username: string;
  password: string;
  password_set: boolean;
  outbound_proxy: string;
  caller_id: string;
  dids: string[];
  t38_enabled: boolean;
  fax_preference_header: boolean;
  codecs: string;
  external_address: string;
}

export type SipRegistration = 'registered' | 'not_registered' | 'rejected' | 'not_used' | 'unknown';
export type SipReachability = 'reachable' | 'unreachable' | 'unknown';

export interface SipTrunkStatus {
  configured: boolean;
  preset?: string;
  preset_label?: string;
  auth?: SipAuthMode;
  host?: string;
  missing?: string[];
  dids?: string[];
  applied: boolean;
  asterisk_connected: boolean;
  registration: SipRegistration;
  registration_text: string;
  reachability: SipReachability;
  reachability_text: string;
  // The transport Asterisk registered over (or, for IP sign-in, the trunk's transport).
  registration_transport?: 'udp' | 'tcp' | 'tls' | null;
  round_trip_ms?: number | null;
  // What STUN shows from Faxbot's network; one plain sentence each.
  internet_address?: string | null;
  behind_router?: boolean | null;
  port_numbers?: 'preserved' | 'consistent' | 'changes' | null;
  public_address_text?: string | null;
  ports_text?: string | null;
  last_call_text?: string | null;
  last_call_at?: string | null;
  // The address Asterisk advertised at its last start, and whether that is out of date.
  advertised_address?: string | null;
  address_changed?: boolean;
  last_call_verdict?: string | null;
  // True after a T.38 call carried no fax data while T.38 is on: offer audio fax for new calls.
  suggest_audio?: boolean;
  // Asterisk shares Faxbot's data folder, so Apply and connect restarts it.
  engine_managed?: boolean;
  // Faxbot asked Asterisk to restart and has not logged in to it again yet.
  engine_restarting?: boolean;
  // The running Asterisk loaded exactly the current trunk settings.
  in_use?: boolean;
  // Whether a fax received over the trunk can reach Faxbot, in one sentence; null when the trunk does not receive.
  handover_ready?: boolean | null;
  handover_text?: string | null;
  message: string;
}

// What Apply and connect did with the fax engine after saving its files.
export type SipEngineAction = 'restarting' | 'current' | 'busy' | 'manual' | 'not_connected' | 'not_allowed';

export interface SipApplyResult {
  ok: true;
  engine?: SipEngineAction;
  message: string;
}

export type SipDisposition = 'answered' | 'busy' | 'congestion' | 'failed' | 'no_answer' | 'ambiguous';

export interface SipCallRecord {
  id: string;
  direction: 'outbound' | 'inbound';
  job_id: string | null;
  attempt_id: string | null;
  trunk_preset: string | null;
  did: string | null;
  caller: string | null;
  called: string | null;
  started_at: string;
  answered_at: string | null;
  ended_at: string | null;
  disposition: SipDisposition;
  connected_seconds: number | null;
  t38: 'yes' | 'no' | 'unknown';
  pages: number | null;
  fax_status: string | null;
  remote_station_id: string | null;
  error_cause: string | null;
  fax_preference: boolean;
  // sent, received, or why a connected call delivered nothing (no_t38_data_back, ...).
  verdict?: string | null;
  // One plain sentence for this call.
  summary?: string;
}

export interface SipCallPage {
  items: SipCallRecord[];
  next_cursor: string | null;
}
