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
  // 'phone_system' for an office phone system on the local network (Avaya IP Office or Aura).
  kind?: 'carrier' | 'phone_system';
  // Transports a person may choose; the first preset default is `transport`.
  transports?: Array<'udp' | 'tcp' | 'tls'>;
  // G.711 order follows the installation country unless a person chooses one.
  codecs_by_country?: boolean;
  // Number formats a person may choose; empty means the preset's own.
  dial_formats?: Array<'e164' | 'local'>;
  // New trunks start with audio fax because the carrier turns T.38 into audio itself.
  audio_by_default?: boolean;
  // What the phone system's administrator sets, in order.
  admin_steps?: string[];
}

// Recipients, Details: one fax machine's own limits and whether it takes SSL Fax (learned from calls).
export interface RecipientFaxLimits {
  number: string;
  accepts_sslfax: boolean | null;
  accepts_sslfax_at: string | null;
  max_rate: number | null;
  ecm: boolean | null;
  sslfax_sentence: string | null;
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
  // How often Faxbot checks its internet address again, in minutes; 0 turns the check off.
  public_address_check_minutes?: number;
  // May Faxbot open its published fax ports on the router (sip_router_ports)?
  router_ports?: boolean;
  // 'e164' or 'local' (as a phone at the installation dials it); '' is the preset's own.
  dial_format?: string;
  // Outside-line digits before a number dialled the local way, such as 9.
  dial_prefix?: string;
  // Fax settings (collapsed on the trunk page); both of Faxbot's fax engines use them.
  t38_error_correction?: 'redundancy' | 'fec' | 'none';
  t38_max_datagram?: number;
  fax_max_rate?: 14400 | 9600 | 7200 | 4800;
  fax_ecm?: boolean;
  fax_compression?: 'mh' | 'mr' | 'mmr' | 'jbig';
  fax_fine?: boolean;
  sslfax_enabled?: boolean;
  fax_lines?: number;
  sslfax_listener_port?: number;
  // Why Faxbot chose audio fax for new calls, and when (read only).
  t38_off_reason?: 'no_data_back' | 'network' | 'carrier' | null;
  t38_off_at?: string | null;
}

export type SipRegistration = 'registered' | 'not_registered' | 'rejected' | 'not_used' | 'unknown';
export type SipReachability = 'reachable' | 'unreachable' | 'unknown';

export interface SipTrunkStatus {
  configured: boolean;
  preset?: string;
  preset_label?: string;
  kind?: 'carrier' | 'phone_system';
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
  // The fast fax service (SSL Fax engine): its state and one sentence; null outside the Compose install.
  engine_state?: 'running' | 'starting' | 'not_set_up' | 'stopped' | null;
  engine_text?: string | null;
  // Why Faxbot chose audio fax for new calls ('no_data_back' or 'network'), and when; null otherwise.
  t38_off_reason?: string | null;
  t38_off_at?: string | null;
  // A phone system: where it reaches Faxbot on the local network, or null while Faxbot is not published there.
  phone_system?: SipPhoneSystemReach | null;
  // While Faxbot is not published on the local network: the command that publishes it and the .env setting.
  phone_system_command?: string | null;
  phone_system_setting?: string | null;
  // Docker Desktop or Colima hides the phone system's address, so it cannot connect from this host.
  phone_system_hidden?: boolean;
  message: string;
}

export interface SipPhoneSystemReach {
  address: string;
  sip_port: number;
  media_ports: string;
  faxes_at_once: number;
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
