// Sending short faxes to the same number together in one call.
import type { Money } from './deliveryTypes';

export interface BatchingChange {
  action: 'on' | 'changed' | 'off';
  by: string;
  at: string;
  recipient_agreed: boolean;
  max_wait_minutes: number;
  max_pages: number;
  mixed_senders: boolean;
}

export interface BatchingNumber {
  number: string;
  enabled: boolean;
  max_wait_minutes: number;
  max_pages: number;
  mixed_senders: boolean;
  version: number;
  saves_money: boolean;
  route_sentence: string;
  state_sentence: string;
  agreement: BatchingChange | null;
  history: BatchingChange[];
  savings: {
    calls: number;
    faxes: number;
    calls_saved: number;
    estimated_saving: Money[];
    is_estimate: boolean;
    sentence: string;
  };
  agreement_text: string;
}

export interface BatchingSave {
  enabled: boolean;
  recipient_agreed?: boolean;
  max_wait_minutes?: number;
  max_pages?: number;
  mixed_senders?: boolean;
  version?: number;
}

export interface BatchingCheck {
  number: string;
  sends_together: boolean;
  wait_minutes: number | null;
  sentence: string | null;
}

// A fax's place in sending together, as Jobs lists it.
export interface FaxTogetherSummary {
  state: 'waiting' | 'together' | 'separate';
  reference: string;
  waiting_until?: string;
  send_now?: boolean;
  documents?: number;
  document_number?: number;
  others?: number;
}

export interface FaxTogether extends Partial<Omit<FaxTogetherSummary, 'state'>> {
  state: FaxTogetherSummary['state'] | null;
  sentence: string | null;
  share?: {
    amount: string;
    call_amount: string;
    currency: string;
    basis: 'reported' | 'estimated';
    sentence: string;
  } | null;
}
