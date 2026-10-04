// One name for each fax provider, used everywhere the console shows one:
// the Setup Wizard, Settings, Dashboard, Jobs, Inbox, delivery routes and
// rate cards. api/app/provider_labels.py keeps the same names for the
// command line, and a test keeps the two lists equal.

export const PROVIDER_LABELS: Record<string, string> = {
  phaxio: 'Phaxio',
  sinch: 'Sinch',
  signalwire: 'SignalWire',
  documo: 'Documo',
  humblefax: 'HumbleFax',
  efax: 'eFax',
  sip: 'SIP trunk (Asterisk)',
  freeswitch: 'SIP trunk (FreeSWITCH)',
};

// Built-in providers in menu order.
export const BUILTIN_PROVIDERS = ['phaxio', 'sinch', 'signalwire', 'documo', 'humblefax', 'efax', 'sip', 'freeswitch'];

// Built-in providers that can receive faxes; the others only send.
export const RECEIVING_PROVIDERS = new Set(['phaxio', 'sinch', 'efax', 'sip']);

export const NO_PROVIDER_LABEL = 'No provider';

// The plain name for a provider id. A provider plugin's own name is used for
// ids Faxbot does not know; an empty id means no provider.
export function providerLabel(id: string | null | undefined, pluginName?: string | null): string {
  const key = String(id ?? '').trim().toLowerCase();
  if (!key) return NO_PROVIDER_LABEL;
  return PROVIDER_LABELS[key] ?? (pluginName?.trim() || key);
}

// "Sending: Phaxio · Receiving: SIP trunk (Asterisk)" for summaries.
export function directionSummary(sending: string, receiving: string): string {
  return `Sending: ${providerLabel(sending)} · Receiving: ${providerLabel(receiving)}`;
}
