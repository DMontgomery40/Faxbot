// The words the Case packets screen uses for a document's state, its acknowledgement and why it is sent.
import type { CaseDocument } from '../../api/deliveryTypes';
import type { CaseDocumentState, CaseWhy } from '../../api/caseTypes';
import { formatServerTime } from '../../api/time';

// Older answers carry only `accepted`; read them as the newer states.
export function documentState(document: CaseDocument): CaseDocumentState {
  return document.state ?? (document.accepted ? 'accepted' : 'sent');
}

export const STATE_LABEL: Record<CaseDocumentState, string> = {
  waiting: 'Waiting to be delivered',
  not_sent: 'Not delivered',
  sent: 'Delivered, not acknowledged',
  accepted: 'Acknowledged',
  expired: 'Acknowledgement too old',
  invalidated: "Recipient couldn't find it",
};

export const STATE_COLOR: Record<CaseDocumentState, 'default' | 'success' | 'warning' | 'error' | 'info'> = {
  waiting: 'default', not_sent: 'error', sent: 'info', accepted: 'success', expired: 'warning', invalidated: 'warning',
};

function how(document: CaseDocument): string {
  switch (document.accepted_how) {
    case 'partner_receipt': return "by the partner's signed receipt";
    case 'work_acknowledged': return 'by the receiving team';
    case 'received_fax': return 'by their acknowledgement fax';
    case 'person': {
      const who = document.accepted_by ? `confirmed by ${document.accepted_by}` : 'confirmed by a person';
      return document.accepted_note ? `${who} ("${document.accepted_note}")` : who;
    }
    default: return '';
  }
}

// One sentence about where the document stands; empty when the label says it all.
export function stateSentence(document: CaseDocument, reuseDays?: number): string {
  const state = documentState(document);
  const when = formatServerTime(document.accepted_at, '');
  switch (state) {
    case 'not_sent':
      return 'The fax that carried it was not delivered; the next packet sends it again.';
    case 'sent':
      return 'Record the recipient\'s confirmation to list it instead of sending it again.';
    case 'accepted': {
      const until = document.expires_at ? `; trusted until ${formatServerTime(document.expires_at)}` : '';
      return `Acknowledged ${when} ${how(document)}${until}.`.replace(/\s+/g, ' ').replace(' .', '.');
    }
    case 'expired':
      return `Acknowledged ${when}, more than ${reuseDays ?? 'the set number of'} days ago; the next packet sends it in full.`;
    case 'invalidated':
      return document.invalidated_note
        ? `They said: "${document.invalidated_note}". The next packet sends it in full.`
        : 'The next packet sends it in full.';
    default:
      return '';
  }
}

// Why a document goes in full in a packet, or that it is listed on the index instead.
export const WHY_LABEL: Record<CaseWhy, string> = {
  new: 'New',
  sent: 'Not acknowledged yet',
  waiting: 'Not delivered yet',
  not_sent: 'Not delivered before',
  expired: 'Acknowledgement too old',
  invalidated: "Recipient couldn't find it",
  references_off: 'Recipient wants every document',
  accepted: 'Acknowledged',
  repair: 'Full packet',
};

// "version final · from Valley EHR", or nothing.
export function versionAndSource(item: { version?: string | null; source?: string | null }): string {
  return [item.version ? `version ${item.version}` : '', item.source ? `from ${item.source}` : '']
    .filter(Boolean).join(' · ');
}

export function pagesText(count: number): string {
  return `${count} ${count === 1 ? 'page' : 'pages'}`;
}
