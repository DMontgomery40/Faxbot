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
}

export interface SipCallPage {
  items: SipCallRecord[];
  next_cursor: string | null;
}
