// Partners → Find partners (GET /direct/discovery and its changes): recipients that run Faxbot, found from fax
// calls, introductions and trusted directories; introductions between partners; publishing your number.

export interface DiscoverySuggestion {
  id: string;
  number: string;
  organization: string;
  source: 'call' | 'introduction' | 'directory';
  endpoint: string;
  directory: string | null;
  created_at: string;
  // "This recipient runs Faxbot. Enroll as a direct partner ..."
  sentence: string;
  // How Faxbot found it, in one sentence.
  source_text: string;
  // A Faxbot on your own network using its own certificate: its fingerprint, and what that means.
  certificate: string | null;
  network_text: string | null;
}

export interface DiscoveryPartner {
  id: string;
  organization: string;
  fax_number: string;
  verified: boolean;
  may_introduce: boolean;
  certificate: string | null;
  certificate_text: string | null;
}

export interface DiscoveryIntroduction {
  id: string;
  when: string;
  first: string;
  second: string;
  sentence: string;
}

export interface DiscoveryPublication {
  id: string;
  number: string;
  directory: string;
  name: string;
  value: string;
  // The zone file line to add, with the value split into strings of at most 255 bytes.
  zone: string;
  expires_at: string;
  expires_text: string;
  expired: boolean;
  sentence: string;
}

export interface DiscoveryLookup {
  when: string;
  kind: 'call' | 'introduction' | 'directory' | 'certificate';
  host: string;
  number: string | null;
  outcome: string;
  organization: string | null;
  certificate: string | null;
  sentence: string;
}

export interface Discovery {
  direct_delivery: boolean;
  settings: { well_known: boolean; from_calls: boolean; directories: string[]; private_allowed: boolean };
  texts: { well_known: string; from_calls: string; directories: string; private: string | null; well_known_url: string };
  suggestions: DiscoverySuggestion[];
  partners: DiscoveryPartner[];
  introductions: DiscoveryIntroduction[];
  publications: DiscoveryPublication[];
  publishable: { number: string | null; receives: boolean; sentence: string };
  lookups: DiscoveryLookup[];
}

export interface DiscoverySettingsChange {
  well_known?: boolean;
  from_calls?: boolean;
  directories?: string[];
}
