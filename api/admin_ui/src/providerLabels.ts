// One name for each fax provider, used everywhere the console shows one:
// the Setup Wizard, Settings, Overview, Sent, Received, delivery routes and
// rate cards. api/app/provider_labels.py keeps the same names for the
// command line, and a test keeps the two lists equal.
//
// The fax line Faxbot runs itself is named after the carrier or phone system it
// connects to ("Telnyx", "Avaya IP Office"); the console context says which,
// and "Carrier trunk" is the name only until it does.

export const PROVIDER_LABELS: Record<string, string> = {
  phaxio: 'Phaxio',
  sinch: 'Sinch',
  signalwire: 'SignalWire',
  documo: 'Documo',
  humblefax: 'HumbleFax',
  efax: 'eFax',
  sip: 'Carrier trunk',
  freeswitch: 'FreeSWITCH',
};

// Built-in providers in menu order.
export const BUILTIN_PROVIDERS = ['phaxio', 'sinch', 'signalwire', 'documo', 'humblefax', 'efax', 'sip', 'freeswitch'];

// Built-in providers that can receive faxes; the others only send.
export const RECEIVING_PROVIDERS = new Set(['phaxio', 'sinch', 'efax', 'sip']);

export const NO_PROVIDER_LABEL = 'No provider';

// Names this installation gives its providers, such as the trunk's carrier ({ sip: 'Telnyx' }).
let installationNames: Record<string, string> = {};

export function setProviderNames(names: Record<string, string> | null | undefined): void {
  installationNames = { ...(names ?? {}) };
}

// The plain name for a provider id. A provider plugin's own name is used for
// ids Faxbot does not know; an empty id means no provider.
export function providerLabel(id: string | null | undefined, pluginName?: string | null): string {
  const key = String(id ?? '').trim().toLowerCase();
  if (!key) return NO_PROVIDER_LABEL;
  return installationNames[key] ?? PROVIDER_LABELS[key] ?? (pluginName?.trim() || key);
}

// "Sending: HumbleFax · Receiving: Telnyx" for summaries.
export function directionSummary(sending: string, receiving: string): string {
  return `Sending: ${providerLabel(sending)} · Receiving: ${providerLabel(receiving)}`;
}
