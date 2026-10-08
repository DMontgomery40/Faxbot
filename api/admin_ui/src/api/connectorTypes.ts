// Intake connectors (/intake/sources): mailboxes and folders that bring documents in or send faxes, once each.

export type ConnectorKind = 'email' | 'folder';
export type ConnectorDirection = 'receive' | 'send';

export interface ConnectorCounts {
  items: number;
  duplicates: number;
  refused: number;
  failed: number;
}

export interface ConnectorSender {
  address: string;
  principal_id: string;
  name?: string | null;
  enabled?: boolean;
}

export interface Connector {
  id: string;
  name: string;
  kind: ConnectorKind;
  direction: ConnectorDirection;
  what: string;
  enabled: boolean;
  paused: boolean;
  status: string;
  ok: boolean | null;
  last_checked_at: string | null;
  has_secret: boolean;
  has_sending_key: boolean;
  sending_key_id: string | null;
  settings: Record<string, string | number | null>;
  version: number;
  created_at: string;
  counts: ConnectorCounts;
  guidance?: string;
  checks_senders_with?: string | null;
  senders?: ConnectorSender[];
  mailbox?: { id: string; label: string; number: string | null };
}

export interface ConnectorItem {
  id: string;
  connector_id: string;
  connector: string | null;
  direction: ConnectorDirection;
  state: string;
  status: string;
  what: string | null;
  sender: string | null;
  sender_name: string | null;
  to_number: string | null;
  duplicates: number;
  refused: boolean;
  fax_id: string | null;
  inbound_id: string | null;
  reply: string | null;
  reply_detail: string | null;
  received_at: string | null;
  created_at: string;
}

export interface ConnectorProvider {
  id: 'microsoft365' | 'google' | 'other';
  guidance: string;
  imap_host?: string;
  imap_port?: number;
  smtp_host?: string;
  smtp_port?: number;
  smtp_security?: string;
  sign_in?: string;
  checks_senders_with?: string | null;
}

export interface ConnectorChoices {
  mailboxes: { id: string; label: string; number: string | null }[];
  people: { id: string; name: string; login: string | null }[];
  providers: ConnectorProvider[];
}

export type ConnectorSettings = Record<string, string | number | null>;

export interface ConnectorInput {
  name: string;
  kind: ConnectorKind;
  direction: ConnectorDirection;
  settings: ConnectorSettings;
  secret?: Record<string, string> | null;
  senders?: { address: string; principal_id: string }[];
}

export interface ConnectorUpdate {
  version: number;
  name?: string;
  settings?: ConnectorSettings;
  secret?: Record<string, string> | null;
  senders?: { address: string; principal_id: string }[];
}

export interface FaxRequester {
  fax_id: string;
  connector: string;
  person: string | null;
  address: string | null;
  sentence: string;
}
