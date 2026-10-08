// Faxes → Expected: faxes recorded before they arrive. Faxbot matches what arrives by a reference the
// sender stated, proposes anything less certain for you to confirm, and shows what is still missing.
// Import brings in open work from another system; Outages sorts the work done while that system was down.
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, List,
  ListItem, ListItemText, MenuItem, Paper, Stack, Tab, Table, TableBody, TableCell, TableContainer, TableHead,
  TableRow, Tabs, TextField, ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import type AdminAPIClient from '../../api/client';
import type {
  ExpectedCounts, ExpectedEvent, ExpectedFax, ExpectedProposal, ExpectedReport, ExpectedView,
} from '../../api/expectedTypes';
import type { InboundFax } from '../../api/types';
import { ScreenHeader } from '../access/AccessViews';
import { DeliveryError, Notice } from '../delivery/shared';
import { maskNumber, shortTime } from '../work/text';
import { expectedApi, type ExpectedApi } from './expectedApi';
import ExpectedImport from './ExpectedImport';
import ExpectedOutages from './ExpectedOutages';

export type ExpectedTab = 'waiting' | 'report' | 'import' | 'outages';

export const OPERATIONAL_TARGET = 'This is an operational target, not a legal deadline.';

const VIEW_LABELS: Array<[ExpectedView, string]> = [
  ['waiting', 'Waiting'], ['overdue', 'Overdue'], ['proposed', 'To confirm'], ['missing', 'Missing from the export'],
  ['conflicts', 'Changed in the import'], ['closed', 'Closed'], ['all', 'All'],
];

// The server's sentence, with the due time written again in the reader's local time.
export function expectedStateSentence(item: Pick<ExpectedFax, 'state_key' | 'state_text' | 'due_at'>): string {
  if (item.state_key === 'waiting' && item.due_at) return `Waiting; expected by ${shortTime(item.due_at)}.`;
  return item.state_text;
}

export function countsSentence(counts: ExpectedCounts): string {
  const waiting = counts.waiting === 1 ? '1 expected fax is waiting' : `${counts.waiting} expected faxes are waiting`;
  return `${waiting}; ${counts.overdue} overdue, ${counts.proposed} to confirm.`;
}

function faxLine(fax: ExpectedProposal['fax']): string {
  if (!fax) return 'A received fax you cannot open.';
  const from = fax.from_number ? `From ${maskNumber(fax.from_number)}` : 'From an unknown number';
  return `${from}, received ${shortTime(fax.received_at)}${fax.mailbox ? ` in ${fax.mailbox}` : ''}.`;
}

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

// -- Expect a fax ------------------------------------------------------------------------------

function ExpectDialog({ api, onClose, onAdded }: {
  api: ExpectedApi;
  onClose: () => void;
  onAdded: (item: ExpectedFax) => void;
}) {
  const [mailboxes, setMailboxes] = useState<Array<{ id: string; label: string }> | null>(null);
  const [form, setForm] = useState({
    reference: '', kind: '', mailbox_id: '', counterparty: '', fax_numbers: '', due: '', revision: '', parts: '',
    signed: false, subaddress: '', email_subject: '', message_id: '', description: '',
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    api.mailboxes().then((found) => {
      setMailboxes(found.mailboxes);
      if (found.mailboxes.length === 1) setForm((current) => ({ ...current, mailbox_id: found.mailboxes[0].id }));
    }).catch(setError);
  }, [api]);
  const set = (name: keyof typeof form) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setForm((current) => ({ ...current, [name]: name === 'signed' ? event.target.checked : event.target.value }));
  const ready = form.reference.trim() && form.kind.trim() && form.mailbox_id;

  const submit = async () => {
    setBusy(true);
    setError(null);
    const parts = form.parts.split(';').map((part) => part.trim()).filter(Boolean);
    if (form.signed && !parts.includes('Signature')) parts.push('Signature');
    const due = form.due ? new Date(form.due) : null;
    try {
      const item = await api.add({
        reference: form.reference.trim(), kind: form.kind.trim(), mailbox_id: form.mailbox_id,
        counterparty: form.counterparty.trim() || undefined,
        fax_numbers: form.fax_numbers.split(/[;,]/).map((number) => number.trim()).filter(Boolean),
        due_at: due && !Number.isNaN(due.getTime()) ? due.toISOString() : undefined,
        required_revision: form.revision.trim() || undefined, required_parts: parts,
        subaddress: form.subaddress.trim() || undefined, email_subject: form.email_subject.trim() || undefined,
        message_id: form.message_id.trim() || undefined, description: form.description.trim() || undefined,
      });
      onAdded(item);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>Expect a fax</DialogTitle>
      <DialogContent>
        <DeliveryError error={error} onClose={() => setError(null)} />
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField label="Reference" value={form.reference} onChange={set('reference')} required
            helperText="The business reference, such as PO 483 or records request 882." />
          <TextField label="What you expect" value={form.kind} onChange={set('kind')} required
            helperText="Such as Signed acknowledgement or Completed referral form." />
          <TextField select label="Mailbox" value={form.mailbox_id} onChange={set('mailbox_id')} required
            helperText={mailboxes && mailboxes.length === 0 ? 'You may not add expected faxes to any mailbox.' : 'The team that owns it.'}>
            {(mailboxes ?? []).map((box) => <MenuItem key={box.id} value={box.id}>{box.label}</MenuItem>)}
          </TextField>
          <TextField label="From" value={form.counterparty} onChange={set('counterparty')}
            helperText="Who will send it, such as Acme Supply." />
          <TextField label="Their fax numbers" value={form.fax_numbers} onChange={set('fax_numbers')}
            helperText="A fax from one of these numbers is shown to you as a possible match; separate them with commas." />
          <TextField label="Due" type="datetime-local" value={form.due} onChange={set('due')}
            InputLabelProps={{ shrink: true }}
            helperText={`Leave empty to use the mailbox's acknowledgement target. ${OPERATIONAL_TARGET}`} />
          <TextField label="Revision it must be" value={form.revision} onChange={set('revision')}
            helperText="Only a fax that states this revision closes it by itself." />
          <TextField label="It must include" value={form.parts} onChange={set('parts')}
            helperText="Parts to check by eye, separated with semicolons. Faxbot then always asks you to confirm." />
          <FormControlLabel control={<Checkbox checked={form.signed} onChange={set('signed')} />}
            label="It must be signed" />
          <Typography variant="subtitle2">Where the reference will appear</Typography>
          <TextField label="Subaddress" value={form.subaddress} onChange={set('subaddress')}
            helperText="Digits the sender dials after your number. Left empty, a reference made only of digits is used." />
          <TextField label="Email subject contains" value={form.email_subject} onChange={set('email_subject')}
            helperText="For documents that arrive by email, such as PO 483." />
          <TextField label="Message ID" value={form.message_id} onChange={set('message_id')}
            helperText="The Direct or email message it answers, when you know it." />
          <TextField label="Note" value={form.description} onChange={set('description')} multiline minRows={2} />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={!ready || busy} onClick={submit}>Expect it</Button>
      </DialogActions>
    </Dialog>
  );
}

// -- One expected fax ----------------------------------------------------------------------------

function NoteDialog({ title, label, action, onClose, onSubmit }: {
  title: string;
  label: string;
  action: string;
  onClose: () => void;
  onSubmit: (note: string) => Promise<void>;
}) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>
        <TextField autoFocus fullWidth sx={{ mt: 1 }} label={label} value={note}
          onChange={(event) => setNote(event.target.value)} inputProps={{ maxLength: 300 }} />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Back</Button>
        <Button variant="contained" disabled={!note.trim() || busy}
          onClick={async () => { setBusy(true); try { await onSubmit(note.trim()); } finally { setBusy(false); } }}>
          {action}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function Proposals({ item, onConfirm, onReject }: {
  item: ExpectedFax;
  onConfirm: (proposal: ExpectedProposal) => void;
  onReject: (proposal: ExpectedProposal) => void;
}) {
  if (!item.proposals.length) return null;
  return (
    <List dense disablePadding>
      {item.proposals.map((proposal) => (
        <ListItem key={proposal.id} disableGutters alignItems="flex-start">
          <ListItemText primary={faxLine(proposal.fax)} secondary={proposal.text} />
          {proposal.can_decide && (
            <Stack direction="row" spacing={1} sx={{ ml: 1, flexShrink: 0 }}>
              <Button size="small" variant="contained" onClick={() => onConfirm(proposal)}>This is it</Button>
              <Button size="small" onClick={() => onReject(proposal)}>Not this one</Button>
            </Stack>
          )}
        </ListItem>
      ))}
    </List>
  );
}

function ExpectedDetail({ api, client, code, onClose, onChanged }: {
  api: ExpectedApi;
  client: AdminAPIClient;
  code: string;
  onClose: () => void;
  onChanged: (message: string) => void;
}) {
  const [item, setItem] = useState<ExpectedFax | null>(null);
  const [events, setEvents] = useState<ExpectedEvent[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [asking, setAsking] = useState<null | 'cancel' | 'elsewhere' | 'reject'>(null);
  const [rejecting, setRejecting] = useState<ExpectedProposal | null>(null);
  const [linking, setLinking] = useState<InboundFax[] | null>(null);
  const [chosenFax, setChosenFax] = useState('');

  const load = useCallback(async () => {
    try {
      const [found, history] = await Promise.all([api.detail(code), api.history(code)]);
      setItem(found);
      setEvents(history.events);
    } catch (failure) {
      setError(failure);
    }
  }, [api, code]);
  useEffect(() => { void load(); }, [load]);

  const run = async (work: () => Promise<unknown>, message: string) => {
    setError(null);
    try {
      await work();
      onChanged(message);
      await load();
    } catch (failure) {
      setError(failure);
    }
  };
  const has = (action: ExpectedFax['actions'][number]) => item?.actions.includes(action);

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>{item ? `${item.reference}: ${item.kind}` : 'Expected fax'}</DialogTitle>
      <DialogContent>
        <DeliveryError error={error} onClose={() => setError(null)} />
        {item && (
          <Stack spacing={2}>
            <Typography>{expectedStateSentence(item)}</Typography>
            <Typography variant="body2" color="text.secondary">{item.due_text}</Typography>
            <Table size="small">
              <TableBody>
                {([
                  ['Code', item.code], ['From', item.counterparty], ['Mailbox', item.mailbox],
                  ['Owner', item.owner?.name], ['Their fax numbers', item.fax_numbers.join(', ')],
                  ['Revision', item.required_revision], ['It must include', item.required_parts.join(', ')],
                  ['Subaddress', item.keys.subaddress], ['Email subject contains', item.keys.email_subject],
                  ['Imported from', item.source], ['Replaces', item.replaces], ['Note', item.description],
                ] as Array<[string, string | null | undefined]>).filter(([, value]) => value).map(([label, value]) => (
                  <TableRow key={label}><TableCell sx={{ width: 200 }}>{label}</TableCell><TableCell>{value}</TableCell></TableRow>
                ))}
              </TableBody>
            </Table>
            {item.match && <Alert severity="success">{faxLine(item.match.fax)}</Alert>}
            {item.missing_from_export && (
              <Alert severity="warning">The latest full export no longer lists it. It stays open until you decide.</Alert>
            )}
            {item.conflict && (
              <Alert severity="warning" action={has('resolve_conflict') ? (
                <Stack direction="row" spacing={1}>
                  <Button size="small" onClick={() => run(() => api.conflict(item.code, 'keep', item.version),
                    'Kept the first version.')}>Keep the first version</Button>
                  <Button size="small" onClick={() => run(() => api.conflict(item.code, 'apply', item.version),
                    "Used the import's version.")}>Use the import&apos;s version</Button>
                </Stack>) : undefined}>
                The import changed this row without a new revision.
              </Alert>
            )}
            <Proposals item={item}
              onConfirm={(proposal) => run(() => api.confirm(item.code, proposal.id, item.version), 'Matched.')}
              onReject={(proposal) => { setRejecting(proposal); setAsking('reject'); }} />
            {linking && (
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}>
                <TextField select size="small" sx={{ minWidth: 320 }} label="Received fax" value={chosenFax}
                  onChange={(event) => setChosenFax(event.target.value)}
                  helperText={linking.length ? 'Faxes received in the last few days that you can open.' : 'No received faxes you can open.'}>
                  {linking.map((fax) => (
                    <MenuItem key={fax.id} value={fax.id}>
                      {`From ${fax.fr ? maskNumber(fax.fr) : 'an unknown number'}, ${shortTime(fax.received_at)}${fax.mailbox ? `, ${fax.mailbox}` : ''}`}
                    </MenuItem>
                  ))}
                </TextField>
                <Button variant="contained" disabled={!chosenFax}
                  onClick={() => run(async () => { await api.match(item.code, chosenFax, item.version); setLinking(null); },
                    'Matched.')}>Link it</Button>
              </Stack>
            )}
            <Typography variant="subtitle2">History</Typography>
            <List dense disablePadding>
              {events.map((event, index) => (
                <ListItem key={index} disableGutters>
                  <ListItemText primary={event.text} secondary={shortTime(event.occurred_at)} />
                </ListItem>
              ))}
            </List>
          </Stack>
        )}
      </DialogContent>
      <DialogActions sx={{ flexWrap: 'wrap', gap: 1 }}>
        {item && has('match') && !linking && (
          <Button onClick={async () => {
            try {
              const faxes = await client.listInbound();
              setLinking(faxes.filter((fax) => fax.status !== 'waiting').slice(0, 50));
            } catch (failure) { setError(failure); }
          }}>Link a received fax</Button>
        )}
        {item && has('completed_elsewhere') && <Button onClick={() => setAsking('elsewhere')}>Completed another way</Button>}
        {item && has('cancel') && <Button color="warning" onClick={() => setAsking('cancel')}>Cancel it</Button>}
        {item && has('export') && (
          <Button onClick={() => run(async () => saveBlob(await api.exportEvidence(item.id),
            `faxbot-expected-${item.code}.zip`), 'Evidence downloaded.')}>Download evidence</Button>
        )}
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
      {item && asking === 'cancel' && (
        <NoteDialog title="Cancel this expected fax" label="Why it is no longer expected" action="Cancel it"
          onClose={() => setAsking(null)}
          onSubmit={(note) => run(() => api.close(item.code, 'cancelled', note, item.version), 'Cancelled.')
            .then(() => setAsking(null))} />
      )}
      {item && asking === 'elsewhere' && (
        <NoteDialog title="Completed another way" label="How it was completed, such as on the supplier's portal"
          action="Record it" onClose={() => setAsking(null)}
          onSubmit={(note) => run(() => api.close(item.code, 'completed_elsewhere', note, item.version), 'Recorded.')
            .then(() => setAsking(null))} />
      )}
      {item && asking === 'reject' && rejecting && (
        <NoteDialog title="Not this one" label="Why this fax is not the one" action="Not this one"
          onClose={() => { setAsking(null); setRejecting(null); }}
          onSubmit={(note) => run(() => api.reject(item.code, rejecting.id, item.version, note),
            'Faxbot keeps waiting for the right fax.').then(() => { setAsking(null); setRejecting(null); })} />
      )}
    </Dialog>
  );
}

// -- Waiting -----------------------------------------------------------------------------------------

function WaitingTab({ api, client, onNotice }: { api: ExpectedApi; client: AdminAPIClient; onNotice: (message: string) => void }) {
  const [view, setView] = useState<ExpectedView>('waiting');
  const [search, setSearch] = useState('');
  const [items, setItems] = useState<ExpectedFax[] | null>(null);
  const [counts, setCounts] = useState<ExpectedCounts | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    try {
      const [found, nextCounts] = await Promise.all([api.list(view, search.trim() || undefined), api.counts()]);
      setItems(found.expected);
      setCounts(nextCounts);
    } catch (failure) {
      setError(failure);
    }
  }, [api, view, search]);
  useEffect(() => { void load(); }, [load]);

  return (
    <Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} justifyContent="space-between" sx={{ mb: 2 }}>
        <Typography color="text.secondary">{counts ? countsSentence(counts) : ' '}</Typography>
        <Button variant="contained" onClick={() => setAdding(true)}>Expect a fax</Button>
      </Stack>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ mb: 2 }}>
        <ToggleButtonGroup size="small" exclusive value={view} onChange={(_, next) => { if (next) setView(next); }}
          aria-label="Which expected faxes" sx={{ flexWrap: 'wrap' }}>
          {VIEW_LABELS.map(([key, label]) => <ToggleButton key={key} value={key}>{label}</ToggleButton>)}
        </ToggleButtonGroup>
        <TextField size="small" label="Search references" value={search} onChange={(event) => setSearch(event.target.value)} />
      </Stack>
      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Reference</TableCell><TableCell>What you expect</TableCell><TableCell>From</TableCell>
              <TableCell>Mailbox</TableCell><TableCell>State</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {(items ?? []).map((item) => (
              <TableRow key={item.id} hover sx={{ cursor: 'pointer' }} onClick={() => setOpen(item.code)}>
                <TableCell>{item.reference}</TableCell>
                <TableCell>{item.kind}</TableCell>
                <TableCell>{item.counterparty || '-'}</TableCell>
                <TableCell>{item.mailbox || '-'}</TableCell>
                <TableCell>
                  <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                    <span>{expectedStateSentence(item)}</span>
                    {item.overdue && <Chip size="small" color="error" label="Overdue" />}
                    {item.proposals.length > 0 && <Chip size="small" color="warning" label="To confirm" />}
                    {item.missing_from_export && <Chip size="small" label="Not in the latest export" />}
                  </Stack>
                </TableCell>
              </TableRow>
            ))}
            {items && items.length === 0 && (
              <TableRow><TableCell colSpan={5}>No expected faxes in this view.</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>
      {adding && (
        <ExpectDialog api={api} onClose={() => setAdding(false)}
          onAdded={(item) => { setAdding(false); onNotice(`Expecting ${item.reference}.`); void load(); }} />
      )}
      {open && (
        <ExpectedDetail api={api} client={client} code={open} onClose={() => { setOpen(null); void load(); }}
          onChanged={onNotice} />
      )}
    </Box>
  );
}

// -- Report ----------------------------------------------------------------------------------------

function ReportTab({ api }: { api: ExpectedApi }) {
  const [days, setDays] = useState(30);
  const [report, setReport] = useState<ExpectedReport | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let current = true;
    api.report(days).then((found) => { if (current) setReport(found); }).catch((failure) => { if (current) setError(failure); });
    return () => { current = false; };
  }, [api, days]);
  return (
    <Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      <TextField select size="small" label="Period" value={days} onChange={(event) => setDays(Number(event.target.value))} sx={{ mb: 2 }}>
        {[7, 30, 90].map((value) => <MenuItem key={value} value={value}>{`Last ${value} days`}</MenuItem>)}
      </TextField>
      {report && (
        <Stack spacing={2}>
          <Alert severity={report.overdue || report.not_stored ? 'warning' : 'info'}>{report.summary}</Alert>
          <Typography variant="h6" component="h2">Still missing</Typography>
          <List dense disablePadding>
            {report.unmatched_expected.map((item) => (
              <ListItem key={item.id} disableGutters>
                <ListItemText primary={`${item.reference}: ${item.kind}${item.counterparty ? ` from ${item.counterparty}` : ''}`}
                  secondary={expectedStateSentence(item)} />
              </ListItem>
            ))}
            {report.unmatched_expected.length === 0 && <ListItem disableGutters><ListItemText secondary="Nothing is missing." /></ListItem>}
          </List>
          <Typography variant="h6" component="h2">Received faxes that answered nothing</Typography>
          <Typography variant="body2" color="text.secondary">
            Listed apart from the matches, so a missing fax cannot hide behind a good match rate.
          </Typography>
          <List dense disablePadding>
            {report.unmatched_arrivals.map((entry) => (
              <ListItem key={entry.inbound_fax_id} disableGutters>
                <ListItemText primary={`From ${entry.from_number ? maskNumber(entry.from_number) : 'an unknown number'}, ${shortTime(entry.received_at)}${entry.mailbox ? ` in ${entry.mailbox}` : ''}`}
                  secondary={entry.why} />
              </ListItem>
            ))}
            {report.unmatched_arrivals.length === 0 && (
              <ListItem disableGutters><ListItemText secondary="No received fax carried a reference that matched nothing." /></ListItem>
            )}
          </List>
        </Stack>
      )}
    </Box>
  );
}

// -- The page ------------------------------------------------------------------------------------

export default function ExpectedFaxes({ client, canImport, canOutage }: {
  client: AdminAPIClient;
  canImport: boolean;
  canOutage: boolean;
}) {
  const api = useMemo(() => expectedApi(client), [client]);
  const [tab, setTab] = useState<ExpectedTab>('waiting');
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <Box>
      <ScreenHeader title="Expected faxes"
        subtitle="Faxes you are waiting for, recorded before they arrive. Faxbot matches what arrives and shows you what is missing." />
      <Notice message={notice} onClose={() => setNotice(null)} />
      <Tabs value={tab} onChange={(_, next) => setTab(next)} sx={{ mb: 2 }} variant="scrollable" allowScrollButtonsMobile>
        <Tab value="waiting" label="Expected" />
        <Tab value="report" label="What is missing" />
        {canImport && <Tab value="import" label="Import" />}
        {canOutage && <Tab value="outages" label="Outages" />}
      </Tabs>
      {tab === 'waiting' && <WaitingTab api={api} client={client} onNotice={setNotice} />}
      {tab === 'report' && <ReportTab api={api} />}
      {tab === 'import' && canImport && <ExpectedImport api={api} onNotice={setNotice} />}
      {tab === 'outages' && canOutage && <ExpectedOutages api={api} onNotice={setNotice} />}
    </Box>
  );
}
