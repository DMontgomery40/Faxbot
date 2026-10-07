// Registered forms (Faxes → Forms): forms, their immutable versions, and forms sent and received.

export type FormFieldType = 'text' | 'date' | 'checkbox' | 'choice' | 'number' | 'signature';

export interface FormVersionSummary {
  id: string;
  number: number;
  title: string;
  // The form content's SHA-256; partners use it to agree on the exact form. Not shown to people.
  address: string;
  source: 'pdf_acroform' | 'pdf_positions' | 'svg_positions' | 'partner';
  source_text: string;
  pages: number;
  fields: number;
  created_at: string;
}

export interface RegisteredForm {
  id: string;
  name: string;
  origin: 'local' | 'partner';
  versions: FormVersionSummary[];
  latest: FormVersionSummary | null;
}

export interface FormField {
  name: string;
  label: string;
  type: FormFieldType;
  type_text: string;
  page: number;
  required: boolean;
  options?: string[];
  format?: 'MM/DD/YYYY' | 'DD/MM/YYYY' | 'YYYY-MM-DD';
  decimals?: number;
  multiline?: boolean;
  max_length?: number | null;
}

export interface FormVersionDetail extends FormVersionSummary {
  form_id: string;
  form_name: string;
  page_sizes: Array<{ width: number; height: number }>;
  field_list: FormField[];
  has_template: boolean;
  renderer: string;
}

export interface FormImportResult {
  created: boolean;
  message: string;
  version: FormVersionDetail;
}

// A signature value: the picture the person chose, turned into black and white dots by Faxbot.
export type FormValue = string | boolean | { picture: string } | { width: number; height: number; bits: string };

export type FormDeliveryState = 'sending' | 'delivered' | 'mismatch' | 'refused' | 'not_sent' | 'uncertain'
  | 'not_received' | 'faxed' | 'matched';

export interface FormDelivery {
  id: string;
  direction: 'outbound' | 'inbound';
  route: 'direct' | 'fax';
  state: FormDeliveryState;
  status: string;
  detail: string | null;
  partner: string | null;
  fax_number: string | null;
  pages: number;
  form_version_id: string | null;
  form: string | null;
  form_version: number | null;
  fax_id: string | null;
  // Only a person sends the pages as a fax after a direct delivery did not go through.
  can_fax: boolean;
  created_at: string;
  updated_at: string;
  values?: Record<string, FormValue> | null;
  // False when this person may see the form but not open what was filled in (document access).
  can_open_values?: boolean;
  fields?: Array<{ name: string; label: string; type: FormFieldType }>;
  message?: string;
}

export interface ReceivedForm extends FormDelivery {
  message_id: string;
  // What the arrival was filed as: the direct delivery's email item, or the received fax.
  intake_item_id: string | null;
  inbound_fax_id: string | null;
  values: Record<string, FormValue> | null;
  fields: Array<{ name: string; label: string; type: FormFieldType }>;
}

export interface PartnerForms {
  reached: boolean;
  message: string;
  forms: Array<{ address: string; title: string; version: number | null; also_here: boolean }>;
}

export interface SendFormRequest {
  version_id: string;
  to: string;
  values: Record<string, FormValue>;
  route?: 'auto' | 'fax';
}
