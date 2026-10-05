// The network check for fax over IP (/admin/sip/network): whether T.38 fax data can come back to Faxbot,
// where Faxbot runs, what Faxbot did about it, and the fix when Faxbot cannot fix it itself.
export interface SipNetworkReport {
  // False for a phone system on the local network or when no carrier trunk is set up; then only `text`.
  applies: boolean;
  checked: boolean;
  checked_at?: string | null;
  platform?: string | null;
  platform_text?: string | null;
  ports?: 'kept' | 'changed_same' | 'changed_per_destination' | 'changed' | null;
  internet_address?: string | null;
  shared_address?: boolean;
  t38: 'open' | 'blocked' | 'unknown' | null;
  why?: string | null;
  // One sentence: whether T.38 can work through this network, and why.
  text: string | null;
  // The same in the Overview's and System diagnostics' words.
  office_text?: string | null;
  // When it can: T.38 is tried first, and audio carries the fax when the carrier declines it.
  tries_text?: string | null;
  // Faxbot's fax ports on the router: whether Faxbot may open them, what happened, and one sentence about it.
  router_ports_enabled?: boolean;
  router_state?: 'open' | 'refused' | 'behind_another_router' | 'off' | 'not_needed' | 'no_fax_ports' | 'no_router' | null;
  router_text?: string | null;
  fax_ports_published?: boolean;
  fix_text?: string | null;
  // Commands to copy, one per line.
  fix_steps?: string[];
  fix_note?: string | null;
  audio_text?: string | null;
  t38_enabled?: boolean;
  // What Faxbot did to T.38 because of the network, and when.
  action?: 'turned_off' | 'turned_on' | null;
  action_at?: string | null;
  fax_ports?: string;
  // After Check again: whether Faxbot switched T.38 for new calls, and what the fax engine did.
  switched?: 't38' | 'audio' | null;
  engine_message?: string | null;
}
