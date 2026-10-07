// Sending short faxes to the same number together in one call.
import type { Money } from './deliveryTypes';

// How a shared call marks where each document starts: a separator page before each document,
// one index page listing each document's pages, or a line at the top of every page.
export type Boundaries = 'separators' | 'index_page' | 'page_headers';

export interface BatchingChange {
  action: 'on' | 'changed' | 'off';
  by: string;
  at: string;
  recipient_agreed: boolean;
  max_wait_minutes: number;
  max_pages: number;
  mixed_senders: boolean;
  boundaries?: Boundaries;
  boundaries_agreed?: boolean;
}

export interface BoundariesChoice {
  value: Boundaries;
  label: string;
  // The recipient's agreement to record before using it; null for separators.
  agreement_text: string | null;
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
    // Separator pages an index page or page marks left out, counted apart from calls saved.
    separator_pages?: {
      calls: number;
      pages_saved: number;
      estimated_saving: Money[];
      is_estimate: boolean;
      sentence: string;
    };
  };
  agreement_text: string;
  // Optional for servers before migration 0025.
  boundaries?: Boundaries;
  boundaries_sentence?: string | null;
  boundaries_agreement?: BatchingChange | null;
  boundaries_choices?: BoundariesChoice[];
  boundaries_keeps?: string;
}

export interface BatchingSave {
  enabled: boolean;
  recipient_agreed?: boolean;
  max_wait_minutes?: number;
  max_pages?: number;
  mixed_senders?: boolean;
  boundaries?: Boundaries;
  boundaries_agreed?: boolean;
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
  // How its call marked documents, and this fax's own pages in that call.
  layout?: Boundaries;
  call_first_page?: number;
  call_last_page?: number;
}

export interface FaxTogether extends Partial<Omit<FaxTogetherSummary, 'state'>> {
  state: FaxTogetherSummary['state'] | null;
  sentence: string | null;
  // One sentence on how its call marked it (separator page, index page line, or page marks).
  layout_sentence?: string | null;
  share?: {
    amount: string;
    call_amount: string;
    currency: string;
    basis: 'reported' | 'estimated';
    sentence: string;
  } | null;
}
