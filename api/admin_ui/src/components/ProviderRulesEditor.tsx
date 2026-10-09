// Add or change one rule. A routing rule says how a fax is sent; a limit narrows what every matching
// fax may use. Conditions are rows ("Destination country is GB, IE"); the dialog shows the rule as the
// sentence the list will show. The condition ids match the faxbot command's --when fields.
import { useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle, Divider, FormControl,
  FormControlLabel, FormGroup, IconButton, InputLabel, ListItemText, MenuItem, Paper, Select, Stack, TextField,
  Tooltip, Typography,
} from '@mui/material';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward';
import ArrowDownwardIcon from '@mui/icons-material/ArrowDownward';
import AddIcon from '@mui/icons-material/Add';
import { countryName } from './common/numbers';
import type {
  Actions, AlternateNumber, Choices, Conditions, Day, PageLayout, Rule, RuleKind, RulesDocument, ScopeKind,
  TimeCondition,
} from './ProviderRulesApi';
import { DAYS, documentLabels, recipientLists } from './ProviderRulesApi';
import type { Names } from './ProviderRulesText';
import {
  ALTERNATE_NOTE, DIGITAL_LABEL, ENCRYPTION_NOTE, LAYOUT_NOTE, SUBADDRESS_NOTE, SUBADDRESS_PATTERN, ruleSentence,
} from './ProviderRulesText';

type Option = { value: string; label: string };
type FieldKind = 'text-list' | 'choice-list' | 'flag' | 'count' | 'megabytes' | 'time';
type Block = 'destination' | 'sender' | 'document' | null;

interface FieldSpec {
  id: string;
  label: string;
  kind: FieldKind;
  block: Block;
  key: string;
  help?: string;
  options?: (context: EditorContext) => Option[];
}

interface EditorContext { document: RulesDocument; choices: Choices }

// Every condition a rule can have, in the order the sentence reads them.
export const CONDITION_FIELDS: FieldSpec[] = [
  { id: 'to-number', label: 'Fax number is', kind: 'text-list', block: 'destination', key: 'numbers',
    help: 'Separate several numbers with commas.' },
  { id: 'to-list', label: 'Number is in recipient group', kind: 'choice-list', block: 'destination', key: 'lists',
    options: ({ document }) => Object.entries(recipientLists(document)).map(([key, list]) => ({ value: key, label: list.name })) },
  { id: 'to-prefix', label: 'Number starts with', kind: 'text-list', block: 'destination', key: 'prefixes',
    help: 'Such as +4420. Separate several with commas.' },
  { id: 'to-country', label: 'Destination country is', kind: 'text-list', block: 'destination', key: 'countries',
    help: 'Two-letter country codes, such as GB, IE.' },
  { id: 'to-region', label: 'Number is in region', kind: 'choice-list', block: 'destination', key: 'regions',
    options: ({ document }) => Object.entries(document.regions ?? {}).map(([key, region]) => ({ value: key, label: region.name })) },
  { id: 'to-recipient', label: 'Saved recipient is', kind: 'choice-list', block: 'destination', key: 'recipients',
    options: ({ choices }) => (choices.recipients ?? []).map((recipient) => ({ value: recipient.id, label: recipient.name })) },
  { id: 'partner', label: 'Number has a verified partner', kind: 'flag', block: 'destination', key: 'partner' },
  { id: 'own-number', label: 'Number is one of your own', kind: 'flag', block: 'destination', key: 'own_number' },
  { id: 'approved-alternate', label: 'Recipient has an approved alternate number', kind: 'flag', block: 'destination',
    key: 'approved_alternate' },
  { id: 'in-site-country', label: "Destination is in the sender's site's country", kind: 'flag', block: 'destination',
    key: 'in_sender_country' },
  { id: 'from-person', label: 'Sender is', kind: 'choice-list', block: 'sender', key: 'people',
    options: ({ choices }) => choices.people.map((person) => ({ value: person.id, label: person.name })) },
  { id: 'from-key', label: 'Sent with the key', kind: 'choice-list', block: 'sender', key: 'keys',
    options: ({ choices }) => choices.keys.map((key) => ({ value: key.id, label: key.name })) },
  { id: 'from-group', label: 'Sender is in group', kind: 'choice-list', block: 'sender', key: 'groups',
    options: ({ choices }) => choices.groups.map((group) => ({ value: group.id, label: group.name })) },
  { id: 'from-mailbox', label: 'Sent from mailbox', kind: 'choice-list', block: 'sender', key: 'mailboxes',
    options: ({ choices }) => choices.mailboxes.map((mailbox) => ({ value: mailbox.id, label: mailbox.name })) },
  { id: 'from-site', label: 'Sent from site', kind: 'choice-list', block: 'sender', key: 'sites',
    options: ({ document }) => (document.sites ?? []).map((site) => ({ value: site.key, label: site.name })) },
  { id: 'workflow', label: 'Part of workflow', kind: 'choice-list', block: null, key: 'workflows',
    options: ({ document }) => (document.workflows ?? []).map((workflow) => ({ value: workflow.key, label: workflow.name })) },
  { id: 'pages-over', label: 'More pages than', kind: 'count', block: 'document', key: 'pages_over' },
  { id: 'pages-under', label: 'Fewer pages than', kind: 'count', block: 'document', key: 'pages_under' },
  { id: 'larger-than-mb', label: 'File larger than (MB)', kind: 'megabytes', block: 'document', key: 'size_over' },
  { id: 'case-packet', label: 'Is a case packet', kind: 'flag', block: 'document', key: 'case_packet' },
  { id: 'urgent', label: 'Marked urgent', kind: 'flag', block: null, key: 'urgent' },
  { id: 'real-call', label: 'Sender asked for a real call', kind: 'flag', block: null, key: 'real_call' },
  { id: 'label', label: 'Labelled', kind: 'choice-list', block: null, key: 'labels',
    options: ({ document }) => documentLabels(document).map((label) => ({ value: label, label })) },
  { id: 'time', label: 'Day and time', kind: 'time', block: null, key: 'time' },
];

const FIELD_BY_ID = new Map(CONDITION_FIELDS.map((field) => [field.id, field]));

type RowValue = string[] | boolean | number | TimeCondition | null;
interface Row { field: string; value: RowValue; text?: string }

function blockOf(conditions: Conditions, block: Block): Record<string, unknown> {
  if (block === null) return conditions as Record<string, unknown>;
  return (conditions[block] ?? {}) as Record<string, unknown>;
}

export function rowsFrom(conditions: Conditions | undefined): Row[] {
  if (!conditions) return [];
  const rows: Row[] = [];
  for (const field of CONDITION_FIELDS) {
    const value = blockOf(conditions, field.block)[field.key];
    if (value === undefined || value === null) continue;
    if (Array.isArray(value) && value.length === 0) continue;
    if (field.kind === 'megabytes') rows.push({ field: field.id, value: (value as number) / 1_000_000 });
    else if (field.kind === 'text-list') rows.push({ field: field.id, value: value as string[], text: (value as string[]).join(', ') });
    else rows.push({ field: field.id, value: value as RowValue });
  }
  return rows;
}

export function conditionsFrom(rows: Row[]): Conditions {
  const conditions: Record<string, unknown> = {};
  for (const row of rows) {
    const field = FIELD_BY_ID.get(row.field);
    if (!field || row.value === null || row.value === undefined) continue;
    if (Array.isArray(row.value) && row.value.length === 0) continue;
    let value: unknown = row.value;
    if (field.kind === 'megabytes') value = Math.round(Number(row.value) * 1_000_000);
    if (field.id === 'to-country') value = (row.value as string[]).map((code) => code.toUpperCase());
    if (field.kind === 'time') {
      const time = row.value as TimeCondition;
      if (!time.days?.length && !time.from && !time.until) continue;
    }
    if (field.block === null) conditions[field.key] = value;
    else conditions[field.block] = { ...(conditions[field.block] as object ?? {}), [field.key]: value };
  }
  return conditions as Conditions;
}

function emptyValue(field: FieldSpec): RowValue {
  switch (field.kind) {
    case 'flag': return true;
    case 'count': return 1;
    case 'megabytes': return 5;
    case 'time': return { days: ['mon', 'tue', 'wed', 'thu', 'fri'], from: '09:00', until: '17:00' };
    default: return [];
  }
}

const DAY_SHORT: Record<Day, string> = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' };

export function TimeEditor({ value, onChange, label }: {
  value: TimeCondition; onChange: (value: TimeCondition) => void; label: string;
}) {
  const days = new Set(value.days ?? []);
  return (
    <Box>
      <FormGroup row aria-label={`${label}: days`}>
        {DAYS.map((day) => (
          <FormControlLabel key={day} label={DAY_SHORT[day]} control={(
            <Checkbox size="small" checked={days.has(day)} onChange={(event) => {
              const next = new Set(days);
              if (event.target.checked) next.add(day); else next.delete(day);
              onChange({ ...value, days: DAYS.filter((item) => next.has(item)) });
            }} />
          )} />
        ))}
      </FormGroup>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}>
        <TextField label="From" type="time" size="small" value={value.from ?? ''} InputLabelProps={{ shrink: true }}
          onChange={(event) => onChange({ ...value, from: event.target.value || undefined })} />
        <TextField label="Until" type="time" size="small" value={value.until ?? ''} InputLabelProps={{ shrink: true }}
          onChange={(event) => onChange({ ...value, until: event.target.value || undefined })} />
        <FormControl size="small" sx={{ minWidth: 220 }}>
          <InputLabel id={`${label}-clock`}>Whose clock</InputLabel>
          <Select labelId={`${label}-clock`} label="Whose clock" value={value.time_zone ?? 'installation'}
            onChange={(event) => onChange({ ...value, time_zone: event.target.value as TimeCondition['time_zone'] })}>
            <MenuItem value="installation">Faxbot's time zone</MenuItem>
            <MenuItem value="sender_site">The sender's site's time zone</MenuItem>
          </Select>
        </FormControl>
      </Stack>
    </Box>
  );
}

function MultiSelect({ label, options, value, onChange, emptyText }: {
  label: string; options: Option[]; value: string[]; onChange: (value: string[]) => void; emptyText?: string;
}) {
  const names = new Map(options.map((option) => [option.value, option.label]));
  if (options.length === 0) {
    return <Typography variant="body2" color="text.secondary">{emptyText ?? 'Nothing to choose yet.'}</Typography>;
  }
  return (
    <FormControl size="small" fullWidth>
      <InputLabel id={`${label}-label`}>{label}</InputLabel>
      <Select multiple labelId={`${label}-label`} label={label} value={value}
        inputProps={{ 'aria-label': label }}
        renderValue={(selected) => (selected as string[]).map((item) => names.get(item) ?? item).join(', ')}
        onChange={(event) => onChange(typeof event.target.value === 'string' ? event.target.value.split(',') : event.target.value)}>
        {options.map((option) => (
          <MenuItem key={option.value} value={option.value}>
            <Checkbox size="small" checked={value.includes(option.value)} />
            <ListItemText primary={option.label} />
          </MenuItem>
        ))}
      </Select>
    </FormControl>
  );
}

function ConditionRow({ row, context, used, onChange, onRemove, group }: {
  row: Row; context: EditorContext; used: Set<string>; onChange: (row: Row) => void; onRemove: () => void; group: string;
}) {
  const field = FIELD_BY_ID.get(row.field) ?? CONDITION_FIELDS[0];
  const fieldLabel = `${group}: condition`;
  let editor: JSX.Element;
  switch (field.kind) {
    case 'text-list':
      editor = (
        <TextField size="small" fullWidth label={field.label} value={row.text ?? ''} helperText={
          field.id === 'to-country' && Array.isArray(row.value) && row.value.length > 0
            ? (row.value as string[]).map((code) => countryName(code.toUpperCase())).join(', ') : field.help}
        onChange={(event) => onChange({ ...row, text: event.target.value,
          value: event.target.value.split(',').map((item) => item.trim()).filter(Boolean) })} />
      );
      break;
    case 'choice-list':
      editor = <MultiSelect label={field.label} options={field.options?.(context) ?? []} value={row.value as string[]}
        onChange={(value) => onChange({ ...row, value })} emptyText="There is nothing to choose here yet." />;
      break;
    case 'flag':
      editor = (
        <FormControl size="small" fullWidth>
          <InputLabel id={`${group}-${field.id}`}>{field.label}</InputLabel>
          <Select labelId={`${group}-${field.id}`} label={field.label} value={row.value ? 'yes' : 'no'}
            inputProps={{ 'aria-label': field.label }}
            onChange={(event) => onChange({ ...row, value: event.target.value === 'yes' })}>
            <MenuItem value="yes">Yes</MenuItem>
            <MenuItem value="no">No</MenuItem>
          </Select>
        </FormControl>
      );
      break;
    case 'count':
    case 'megabytes':
      editor = (
        <TextField size="small" fullWidth type="number" label={field.label} value={row.value as number}
          inputProps={{ min: field.kind === 'count' ? 0 : 0.1, step: field.kind === 'count' ? 1 : 0.1 }}
          onChange={(event) => onChange({ ...row, value: event.target.value === '' ? null : Number(event.target.value) })} />
      );
      break;
    default:
      editor = <TimeEditor label={`${group} time`} value={row.value as TimeCondition}
        onChange={(value) => onChange({ ...row, value })} />;
  }
  return (
    <Stack direction={{ xs: 'column', md: 'row' }} spacing={1} alignItems={{ md: 'flex-start' }} sx={{ mb: 1.5 }}>
      <FormControl size="small" sx={{ minWidth: 260 }}>
        <InputLabel id={`${group}-${row.field}-field`}>Condition</InputLabel>
        <Select labelId={`${group}-${row.field}-field`} label="Condition" value={row.field}
          inputProps={{ 'aria-label': fieldLabel }}
          onChange={(event) => {
            const next = FIELD_BY_ID.get(event.target.value)!;
            onChange({ field: next.id, value: emptyValue(next), text: '' });
          }}>
          {CONDITION_FIELDS.filter((option) => option.id === row.field || !used.has(option.id)).map((option) => (
            <MenuItem key={option.id} value={option.id}>{option.label}</MenuItem>
          ))}
        </Select>
      </FormControl>
      <Box sx={{ flex: 1 }}>{editor}</Box>
      <Tooltip title="Remove this condition">
        <IconButton aria-label={`Remove ${field.label}`} onClick={onRemove}><DeleteOutlineIcon /></IconButton>
      </Tooltip>
    </Stack>
  );
}

function ConditionList({ title, help, rows, context, onChange }: {
  title: string; help: string; rows: Row[]; context: EditorContext; onChange: (rows: Row[]) => void;
}) {
  const used = new Set(rows.map((row) => row.field));
  const next = CONDITION_FIELDS.find((field) => !used.has(field.id));
  return (
    <Box sx={{ mb: 2 }}>
      <Typography variant="subtitle1">{title}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{help}</Typography>
      {rows.map((row, index) => (
        <ConditionRow key={`${row.field}-${index}`} row={row} context={context} used={used} group={title}
          onChange={(changed) => onChange(rows.map((item, at) => (at === index ? changed : item)))}
          onRemove={() => onChange(rows.filter((_, at) => at !== index))} />
      ))}
      {next && (
        <Button size="small" startIcon={<AddIcon />}
          onClick={() => onChange([...rows, { field: next.id, value: emptyValue(next), text: '' }])}>
          {title === 'When' ? 'Add a condition' : 'Add an exception'}
        </Button>
      )}
    </Box>
  );
}

export type RouteMethod = 'use' | 'try_in_order' | 'cheapest_reliable' | 'site_accounts' | 'automatic';
const METHODS: Array<{ value: RouteMethod; label: string }> = [
  { value: 'use', label: 'One account' },
  { value: 'try_in_order', label: 'Accounts in the order I choose' },
  { value: 'cheapest_reliable', label: 'The cheapest reliable of these accounts' },
  { value: 'site_accounts', label: "A site's accounts" },
  { value: 'automatic', label: 'The cheapest reliable route, as before' },
];

// What a routing rule does, from the editor's choices: one way to send plus its settings.
export function routeActions(method: RouteMethod, then: Actions): Actions {
  const settings: Actions = {};
  if (then.when_busy) settings.when_busy = then.when_busy;
  if (then.page_layout) settings.page_layout = then.page_layout;
  if (then.alternate_number) settings.alternate_number = then.alternate_number;
  if (then.subaddress) settings.subaddress = then.subaddress;
  switch (method) {
    case 'use': return { use: then.use ?? '', ...settings };
    case 'try_in_order': return { try_in_order: then.try_in_order ?? [], ...settings };
    case 'cheapest_reliable': return { cheapest_reliable: then.cheapest_reliable ?? [], ...settings };
    case 'site_accounts': return { site_accounts: then.site_accounts ?? 'sender', mode: then.mode ?? 'cheapest_reliable', ...settings };
    default: return { automatic: true, ...settings };
  }
}

// What a limit does, from the editor's choices.
export function limitActions(then: Actions, capText: string): Actions {
  const next: Actions = {};
  if (then.never?.length) next.never = then.never;
  if (then.require_direct) next.require_direct = true;
  if (then.require_encryption) next.require_encryption = true;
  if (then.cap_cost) next.cap_cost = { currency: then.cap_cost.currency, amount: capText.trim() };
  if (then.hold_for_approval) next.hold_for_approval = then.hold_for_approval;
  if (then.hold_until) next.hold_until = then.hold_until;
  if (then.place_a_real_call) next.place_a_real_call = true;
  if (then.alternate_number === 'never') next.alternate_number = 'never';
  return next;
}

function methodOf(then: Actions): RouteMethod {
  if (then.try_in_order) return 'try_in_order';
  if (then.cheapest_reliable) return 'cheapest_reliable';
  if (then.site_accounts) return 'site_accounts';
  if (then.automatic) return 'automatic';
  return 'use';
}

function OrderedAccounts({ value, options, onChange }: { value: string[]; options: Option[]; onChange: (value: string[]) => void }) {
  const names = new Map(options.map((option) => [option.value, option.label]));
  const remaining = options.filter((option) => !value.includes(option.value));
  return (
    <Box>
      {value.map((key, index) => (
        <Stack key={key} direction="row" alignItems="center" spacing={1}>
          <Typography variant="body2" sx={{ minWidth: 24 }}>{index + 1}.</Typography>
          <Typography variant="body2" sx={{ flex: 1 }}>{names.get(key) ?? key}</Typography>
          <IconButton size="small" aria-label={`Move ${names.get(key) ?? key} up`} disabled={index === 0}
            onClick={() => { const next = [...value]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; onChange(next); }}>
            <ArrowUpwardIcon fontSize="small" />
          </IconButton>
          <IconButton size="small" aria-label={`Move ${names.get(key) ?? key} down`} disabled={index === value.length - 1}
            onClick={() => { const next = [...value]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; onChange(next); }}>
            <ArrowDownwardIcon fontSize="small" />
          </IconButton>
          <IconButton size="small" aria-label={`Remove ${names.get(key) ?? key}`}
            onClick={() => onChange(value.filter((item) => item !== key))}>
            <DeleteOutlineIcon fontSize="small" />
          </IconButton>
        </Stack>
      ))}
      {remaining.length > 0 && (
        <FormControl size="small" sx={{ minWidth: 240, mt: 1 }}>
          <InputLabel id="add-account-in-order">Add an account</InputLabel>
          <Select labelId="add-account-in-order" label="Add an account" value=""
            inputProps={{ 'aria-label': 'Add an account to the order' }}
            onChange={(event) => onChange([...value, event.target.value])}>
            {remaining.map((option) => <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>)}
          </Select>
        </FormControl>
      )}
      {value.length > 3 && (
        <Typography variant="body2" color="warning.main" sx={{ mt: 1 }}>
          Faxbot makes at most three attempts at one fax, so accounts after the third are used only when the earlier
          ones can't take the fax at all.
        </Typography>
      )}
    </Box>
  );
}

function SelectField({ label, value, options, onChange, help }: {
  label: string; value: string; options: Option[]; onChange: (value: string) => void; help?: string;
}) {
  const id = `rule-${label.replace(/\W+/g, '-').toLowerCase()}`;
  return (
    <FormControl size="small" fullWidth>
      <InputLabel id={id}>{label}</InputLabel>
      <Select labelId={id} label={label} value={value} inputProps={{ 'aria-label': label }}
        onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>)}
      </Select>
      {help && <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>{help}</Typography>}
    </FormControl>
  );
}

export function newRuleId(document: RulesDocument, kind: RuleKind, name: string): string {
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40).replace(/-+$/, '') || 'rule';
  const base = `${kind === 'limits' ? 'l-' : 'r-'}${slug}`;
  const taken = new Set([...(document.limits ?? []), ...(document.routes ?? [])].map((rule) => rule.id));
  let candidate = base;
  for (let count = 2; taken.has(candidate); count += 1) candidate = `${base}-${count}`;
  return candidate;
}

export interface RuleEditorProps {
  open: boolean;
  kind: RuleKind;
  rule: Rule | null;
  // Where the lists, regions, sites, workflows and labels come from: the organization's rules, for a
  // mailbox's or a workflow's own rules. The document being edited when absent.
  definitions?: RulesDocument;
  document: RulesDocument;
  choices: Choices;
  names: Names;
  scopeKind: ScopeKind;
  // The installation's currency, for a cost cap.
  currency: string;
  saving?: boolean;
  onClose: () => void;
  onSave: (rule: Rule) => void;
}

export default function ProviderRulesEditor(props: RuleEditorProps) {
  const { open, kind, rule, document, definitions, choices, names, scopeKind, currency, saving, onClose, onSave } = props;
  const named = definitions ?? document;
  const context = useMemo(() => ({ document: named, choices }), [named, choices]);
  const [name, setName] = useState('');
  const [when, setWhen] = useState<Row[]>([]);
  const [unless, setUnless] = useState<Row[]>([]);
  const [then, setThen] = useState<Actions>({});
  const [method, setMethod] = useState<RouteMethod>('use');
  const [mandatory, setMandatory] = useState(false);
  const [capText, setCapText] = useState('');

  useEffect(() => {
    if (!open) return;
    setName(rule?.name ?? '');
    setWhen(rowsFrom(rule?.when));
    setUnless(rowsFrom(rule?.unless));
    setThen(rule?.then ?? (kind === 'routes' ? {} : {}));
    setMethod(methodOf(rule?.then ?? {}));
    setMandatory(Boolean(rule?.mandatory));
    setCapText(rule?.then.cap_cost?.amount ?? '');
  }, [open, rule, kind]);

  const accounts: Option[] = [
    ...choices.accounts.filter((account) => account.sends).map((account) => ({ value: account.key, label: account.label })),
    { value: 'direct', label: 'Direct delivery' },
    { value: 'digital', label: DIGITAL_LABEL },
  ];
  const siteOptions: Option[] = [{ value: 'sender', label: "The sender's own site" },
    ...(named.sites ?? []).map((site) => ({ value: site.key, label: site.name }))];

  const built: Rule = {
    id: rule?.id ?? newRuleId(document, kind, name),
    name: name.trim(),
    on: rule?.on ?? true,
    ...(scopeKind === 'organization' && mandatory ? { mandatory: true } : {}),
    when: conditionsFrom(when),
    ...(unless.length > 0 && Object.keys(conditionsFrom(unless)).length > 0 ? { unless: conditionsFrom(unless) } : {}),
    then: kind === 'routes' ? routeActions(method, then) : limitActions(then, capText),
  };

  const problem = (() => {
    if (!built.name) return 'Give the rule a name.';
    if (kind === 'routes') {
      if (method === 'use' && !built.then.use) return 'Choose the account.';
      if ((method === 'try_in_order' && !built.then.try_in_order?.length)
        || (method === 'cheapest_reliable' && !built.then.cheapest_reliable?.length)) return 'Choose at least one account.';
      return null;
    }
    if (Object.keys(built.then).length === 0) return 'Choose at least one thing this limit does.';
    if (built.then.cap_cost && !/^\d+(\.\d{1,6})?$/.test(capText.trim())) return 'Write the cost cap as an amount, such as 0.50.';
    return null;
  })();

  const set = (patch: Actions) => setThen((current) => ({ ...current, ...patch }));
  const title = rule ? `Change “${rule.name}”` : kind === 'routes' ? 'Add a routing rule' : 'Add a limit';

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md" aria-labelledby="rule-editor-title">
      <DialogTitle id="rule-editor-title">{title}</DialogTitle>
      <DialogContent dividers>
        <TextField label="Name" fullWidth value={name} onChange={(event) => setName(event.target.value)} sx={{ mb: 2 }}
          helperText="What the rule is for, in your words, such as “UK numbers go through Sinch”." inputProps={{ maxLength: 120 }} />
        <ConditionList title="When" rows={when} context={context} onChange={setWhen}
          help="Every condition must match. With none, the rule applies to every fax." />
        <ConditionList title="Except when" rows={unless} context={context} onChange={setUnless}
          help="The rule does not apply to a fax that matches every exception." />
        <Divider sx={{ my: 2 }} />
        {kind === 'routes' ? (
          <Stack spacing={2}>
            <Typography variant="subtitle1">Send by</Typography>
            <SelectField label="How to send" value={method} options={METHODS}
              onChange={(value) => setMethod(value as RouteMethod)} />
            {method === 'use' && (
              <SelectField label="Account" value={then.use ?? ''} options={accounts} onChange={(value) => set({ use: value })} />
            )}
            {method === 'try_in_order' && (
              <OrderedAccounts value={then.try_in_order ?? []} options={accounts}
                onChange={(value) => set({ try_in_order: value })} />
            )}
            {method === 'cheapest_reliable' && (
              <MultiSelect label="Accounts" options={accounts} value={then.cheapest_reliable ?? []}
                onChange={(value) => set({ cheapest_reliable: value })} />
            )}
            {method === 'site_accounts' && (
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <SelectField label="Site" value={then.site_accounts ?? 'sender'} options={siteOptions}
                  onChange={(value) => set({ site_accounts: value })} />
                <SelectField label="Order" value={then.mode ?? 'cheapest_reliable'}
                  options={[{ value: 'cheapest_reliable', label: 'Cheapest reliable first' },
                    { value: 'ordered', label: 'In the order the site lists them' }]}
                  onChange={(value) => set({ mode: value as Actions['mode'] })} />
              </Stack>
            )}
            <SelectField label="When every line is busy" value={then.when_busy === 'next' ? 'next' : 'wait'}
              options={[{ value: 'wait', label: 'Wait for a free line' }, { value: 'next', label: 'Use the next account' }]}
              onChange={(value) => set({ when_busy: value === 'next' ? 'next' : (rule?.then.when_busy === 'wait' ? 'wait' : undefined) })} />
            <SelectField label="Pages per sheet" value={then.page_layout ?? ''} help={LAYOUT_NOTE}
              options={[{ value: '', label: "Faxbot's usual" },
                { value: 'as_receiver_allows', label: 'As many as the receiving machine allows' },
                { value: 'one_per_sheet', label: 'One page per sheet' }]}
              onChange={(value) => set({ page_layout: (value || undefined) as PageLayout | undefined })} />
            <SelectField label="Approved alternate number" value={then.alternate_number ?? ''} help={ALTERNATE_NOTE}
              options={[{ value: '', label: "Faxbot's usual" },
                { value: 'use', label: 'Dial it when the recipient has one' },
                { value: 'never', label: 'Always dial the number the sender gave' },
                { value: 'only', label: 'Dial only an approved alternate, and hold the fax when there is none' }]}
              onChange={(value) => set({ alternate_number: (value || undefined) as AlternateNumber | undefined })} />
            <TextField size="small" label="Subaddress" value={then.subaddress ?? ''}
              error={Boolean(then.subaddress) && !SUBADDRESS_PATTERN.test(then.subaddress ?? '')}
              helperText={then.subaddress && !SUBADDRESS_PATTERN.test(then.subaddress)
                ? 'A subaddress is up to 20 digits, such as 2001; it may also use +, # and *.' : SUBADDRESS_NOTE}
              inputProps={{ 'aria-label': 'Subaddress', inputMode: 'numeric' }}
              onChange={(event) => set({ subaddress: event.target.value.replace(/ /g, '') || undefined })} />
            {scopeKind === 'organization' && (
              <FormControlLabel control={<Checkbox checked={mandatory} onChange={(event) => setMandatory(event.target.checked)} />}
                label="Mandatory: a mailbox's or workflow's own rules can't replace this rule" />
            )}
          </Stack>
        ) : (
          <Stack spacing={1.5}>
            <Typography variant="subtitle1">This limit</Typography>
            <MultiSelect label="Never use these accounts" options={accounts} value={then.never ?? []}
              onChange={(value) => set({ never: value })} />
            <FormControlLabel label="Send only by direct delivery to a verified partner" control={(
              <Checkbox checked={Boolean(then.require_direct)} onChange={(event) => set({ require_direct: event.target.checked || undefined })} />
            )} />
            <Box>
              <FormControlLabel label="Send only encrypted: direct delivery, or SSL Fax where this number has used it before"
                control={(
                  <Checkbox checked={Boolean(then.require_encryption)}
                    onChange={(event) => set({ require_encryption: event.target.checked || undefined })} />
                )} />
              {then.require_encryption && <Alert severity="info" sx={{ mt: 0.5 }}>{ENCRYPTION_NOTE}</Alert>}
            </Box>
            <Stack direction="row" spacing={1} alignItems="center">
              <FormControlLabel label="Use only routes that cost at most" control={(
                <Checkbox checked={Boolean(then.cap_cost)} onChange={(event) => set({
                  cap_cost: event.target.checked ? { currency, amount: capText || '0' } : undefined })} />
              )} />
              <TextField size="small" label={`Amount (${currency})`} value={capText} disabled={!then.cap_cost}
                onChange={(event) => setCapText(event.target.value)} sx={{ width: 160 }} />
            </Stack>
            {then.cap_cost && (
              <Typography variant="caption" color="text.secondary">
                A route with no known price never fits under a cap, because an unknown price isn't zero.
              </Typography>
            )}
            <FormControlLabel label="Hold the fax until someone approves it" control={(
              <Checkbox checked={Boolean(then.hold_for_approval)}
                onChange={(event) => set({ hold_for_approval: event.target.checked ? {} : undefined })} />
            )} />
            {then.hold_for_approval && (
              <FormControlLabel sx={{ pl: 4 }} label="The approver must be someone other than the sender" control={(
                <Checkbox checked={Boolean(then.hold_for_approval.separate_approver)}
                  onChange={(event) => set({ hold_for_approval: event.target.checked ? { separate_approver: true } : {} })} />
              )} />
            )}
            <FormControlLabel label="Send the fax only at certain times" control={(
              <Checkbox checked={Boolean(then.hold_until)} onChange={(event) => set({
                hold_until: event.target.checked ? { days: ['mon', 'tue', 'wed', 'thu', 'fri'], from: '18:00', until: '07:00' } : undefined })} />
            )} />
            {then.hold_until && (
              <Box sx={{ pl: 4 }}>
                <TimeEditor label="Send only" value={then.hold_until} onChange={(value) => set({ hold_until: value })} />
              </Box>
            )}
            <FormControlLabel label="Place a real call, even to your own numbers" control={(
              <Checkbox checked={Boolean(then.place_a_real_call)}
                onChange={(event) => set({ place_a_real_call: event.target.checked || undefined })} />
            )} />
            <FormControlLabel label="Always dial the number the sender gave, never an approved alternate" control={(
              <Checkbox checked={then.alternate_number === 'never'}
                onChange={(event) => set({ alternate_number: event.target.checked ? 'never' : undefined })} />
            )} />
            {scopeKind === 'organization' && (
              <FormControlLabel control={<Checkbox checked={mandatory} onChange={(event) => setMandatory(event.target.checked)} />}
                label="Mandatory: no one can send a fax anyway around this limit, and mailbox and workflow rules can't loosen it" />
            )}
          </Stack>
        )}
        <Paper variant="outlined" sx={{ p: 2, mt: 3, borderRadius: 2 }}>
          <Typography variant="caption" color="text.secondary">This rule reads</Typography>
          <Typography variant="body2" data-testid="rule-preview">{ruleSentence(built, names)}</Typography>
        </Paper>
      </DialogContent>
      <DialogActions>
        {problem && <Typography variant="body2" color="text.secondary" sx={{ mr: 'auto', pl: 2 }}>{problem}</Typography>}
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={Boolean(problem) || saving} onClick={() => onSave(built)}>Save to draft</Button>
      </DialogActions>
    </Dialog>
  );
}
