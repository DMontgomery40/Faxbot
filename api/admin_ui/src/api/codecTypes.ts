// Encoded pages (experimental): a document carried on a few dense pages that the recipient's Faxbot decodes.

export interface CodecChange {
  action: 'on' | 'changed' | 'off';
  by: string;
  at: string;
  recipient_agreed: boolean;
  style: string;
  fec: string;
  key_fingerprint: string | null;
}

export interface CodecNumber {
  number: string;
  enabled: boolean;
  style: 'dense' | 'picture';
  fec: 'low' | 'medium' | 'high';
  has_key: boolean;
  key_fingerprint: string | null;
  version: number;
  state_sentence: string;
  agreement: CodecChange | null;
  history: CodecChange[];
  agreement_text: string;
  limits_text: string;
}

export interface CodecSave {
  enabled: boolean;
  recipient_agreed?: boolean;
  style?: 'dense' | 'picture';
  fec?: 'low' | 'medium' | 'high';
  shared_key?: string;
  clear_key?: boolean;
  version?: number;
}

export interface CodecReceived {
  encoded: boolean;
  state: 'decoded' | 'failed' | null;
  document_available?: boolean;
  sentence: string | null;
  document_name?: string | null;
  content_type?: string | null;
  size_bytes?: number | null;
}
