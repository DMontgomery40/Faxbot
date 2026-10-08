// Sending only the pages a broken call did not confirm (GET/POST /continuations/faxes/{id}).
import type { CertaintyItem } from './certaintyTypes';

export interface ContinuationMoney {
  amount: string;
  currency: string;
}

export interface ContinuationOffer {
  // False: the fax broke part way, but Faxbot cannot tell which pages arrived; `reason` says why in one sentence.
  available: boolean;
  reason: string | null;
  // How Faxbot knows which pages the receiving machine confirmed, in one sentence.
  basis: string | null;
  confirmed_pages: number | null;
  // An uncertain item still waiting to be settled: the action is on that item, with the person's reason.
  open_item_id: string | null;
  may_send: boolean;
  first_page?: number;
  last_page?: number;
  pages?: number;
  total_pages?: number;
  pages_text?: string;
  action?: string;
  warning?: string;
  cost_text?: string;
  cost_account?: string | null;
  cost?: { part: ContinuationMoney | null; whole: ContinuationMoney | null } | null;
}

export interface ContinuationLink {
  fax_id: string;
  first_page: number;
  last_page: number;
  pages_text: string;
}

export interface ContinuationSent extends ContinuationLink {
  sent_at: string | null;
  requested_by: string | null;
  // True when an uncertain item recorded it: that item's section already links the new fax.
  from_item: boolean;
}

export interface ContinuationView {
  fax_id: string;
  offer: ContinuationOffer | null;
  // This fax's remaining pages went as a new fax.
  continued_by: ContinuationSent | null;
  // This fax carries the remaining pages of an earlier fax.
  continues: ContinuationLink | null;
  // After sending through an uncertain item: the item, settled.
  item?: CertaintyItem | null;
}

// What an uncertain item says about sending only the remaining pages.
export type CertaintyContinuation =
  | { state: 'sent'; fax_id: string; first_page: number; last_page: number; pages_text: string }
  | { state: 'unavailable'; reason: string }
  | {
    state: 'offered'; first_page: number; last_page: number; pages: number; total_pages: number; pages_text: string;
    action: string; basis: string; warning: string; may_send: boolean; cost_text?: string;
  };
