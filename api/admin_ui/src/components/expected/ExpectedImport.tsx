// Faxes → Expected → Import: import sources saved once (the file's format and which column holds
// each field), the file upload, and what each import changed.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, Grid, List, ListItem, ListItemText, MenuItem, Stack, TextField, Typography,
} from '@mui/material';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import {
  IMPORT_FIELDS, type ImportField, type ImportRun, type ImportSource, type ImportSourceInput,
} from '../../api/expectedTypes';
import { DeliveryError } from '../delivery/shared';
import { shortTime } from '../work/text';
import type { ExpectedApi } from './expectedApi';

export const FIELD_LABELS: Record<ImportField, string> = {
  reference: 'Business reference (required)',
  operation_id: "The row's own ID, when it differs from the reference",
  revision: 'Revision',
  kind: 'What is expected',
  description: 'Note',
  counterparty: 'Who will send it',
  fax_numbers: 'Their fax numbers',
  direct_address: 'Their Direct address',
  due: 'Due date or time',
  mailbox: 'Mailbox name',
  subaddress: 'Subaddress',
  email_subject: 'Email subject text',
  message_id: 'Message ID it answers',
  required_parts: 'Parts it must include (separated with semicolons)',
  required_revision: 'Revision it must be',
  window_start: 'When it was requested',
};

function SourceDialog({ api, source, onClose, onSaved }: {
  api: ExpectedApi;
  source: ImportSource | null;
  onClose: () => void;
  onSaved: (saved: ImportSource) => void;
}) {
  const [mailboxes, setMailboxes] = useState<Array<{ id: string; label: string }>>([]);
  const [name, setName] = useState(source?.name ?? '');
  const [format, setFormat] = useState<'csv' | 'json'>(source?.format ?? 'csv');
  const [mapping, setMapping] = useState<Partial<Record<ImportField, string>>>(source?.mapping ?? {});
  const [mailbox, setMailbox] = useState(source?.mailbox_id ?? '');
  const [hours, setHours] = useState(source?.due_hours != null ? String(source.due_hours) : '');
  const [subject, setSubject] = useState(source?.subject_template ?? '');
  const [sub, setSub] = useState(source?.subaddress_template ?? '');
  const [formField, setFormField] = useState(source?.form_field ?? '');
  const [revisionField, setRevisionField] = useState(source?.revision_field ?? '');
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { api.mailboxes().then((found) => setMailboxes(found.mailboxes)).catch(setError); }, [api]);

  const save = async () => {
    setError(null);
    const input: ImportSourceInput = {
      name: name.trim(), format, mapping: Object.fromEntries(Object.entries(mapping).filter(([, column]) => column?.trim())),
      mailbox_id: mailbox || null, due_hours: hours.trim() ? Number(hours) : null,
      subject_template: subject.trim() || null, subaddress_template: sub.trim() || null,
      form_field: formField.trim() || null, revision_field: revisionField.trim() || null,
      ...(source ? { id: source.id, version: source.version } : {}),
    };
    try {
      onSaved(await api.saveSource(input));
    } catch (failure) {
      setError(failure);
    }
  };

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>{source ? `Change ${source.name}` : 'Add an import source'}</DialogTitle>
      <DialogContent>
        <DeliveryError error={error} onClose={() => setError(null)} />
        <Stack spacing={2} sx={{ mt: 1 }}>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
            <TextField label="Name" value={name} onChange={(event) => setName(event.target.value)} fullWidth
              helperText="The system the export comes from, such as Open purchase orders." />
            <TextField select label="File format" value={format} sx={{ minWidth: 180 }}
              onChange={(event) => setFormat(event.target.value as 'csv' | 'json')}>
              <MenuItem value="csv">CSV with a header row</MenuItem>
              <MenuItem value="json">JSON list of rows</MenuItem>
            </TextField>
          </Stack>
          <Typography variant="subtitle2">Which column holds each field</Typography>
          <Typography variant="body2" color="text.secondary">
            Type each column name as it appears in the file. Leave a field empty when the file does not have it.
          </Typography>
          <Grid container spacing={2}>
            {IMPORT_FIELDS.map((field) => (
              <Grid item xs={12} sm={6} key={field}>
                <TextField fullWidth size="small" label={FIELD_LABELS[field]} value={mapping[field] ?? ''}
                  required={field === 'reference'}
                  onChange={(event) => setMapping((current) => ({ ...current, [field]: event.target.value }))} />
              </Grid>
            ))}
          </Grid>
          <TextField select label="Mailbox for rows that name none" value={mailbox}
            onChange={(event) => setMailbox(event.target.value)}>
            <MenuItem value="">None: such rows are reported as problems</MenuItem>
            {mailboxes.map((box) => <MenuItem key={box.id} value={box.id}>{box.label}</MenuItem>)}
          </TextField>
          <TextField label="Hours each row has when it gives no due time" value={hours} type="number"
            onChange={(event) => setHours(event.target.value)}
            helperText="Leave empty to use the mailbox's acknowledgement target. This is an operational target, not a legal deadline." />
          <TextField label="Email subject pattern" value={subject} onChange={(event) => setSubject(event.target.value)}
            helperText="How the reference appears in an email subject, such as PO {reference}. An email whose subject contains it closes the expected fax." />
          <TextField label="Subaddress pattern" value={sub} onChange={(event) => setSub(event.target.value)}
            helperText="How the reference appears in the subaddress the sender's fax machine sends (many machines have a SUB or subaddress field), such as {digits}. Left empty, a reference made only of digits is used." />
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
            <TextField fullWidth label="Partner form field with the reference" value={formField}
              onChange={(event) => setFormField(event.target.value)}
              helperText="For partners who send a registered form directly." />
            <TextField fullWidth label="Partner form field with the revision" value={revisionField}
              onChange={(event) => setRevisionField(event.target.value)} />
          </Stack>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={!name.trim() || !mapping.reference?.trim()} onClick={save}>Save</Button>
      </DialogActions>
    </Dialog>
  );
}

function RunResult({ run }: { run: ImportRun }) {
  return (
    <Stack spacing={1}>
      <Alert severity={run.problem_count ? 'warning' : 'success'}>
        {run.summary}
        {run.replay ? ' This file was imported before; nothing was added twice.' : ''}
        {run.reconciled_outage ? ' It was sorted against the outage of this system; see Outages.' : ''}
      </Alert>
      {run.note && <Alert severity="info">{run.note}</Alert>}
      {run.problems.length > 0 && (
        <List dense disablePadding>
          {run.problems.map((problem) => (
            <ListItem key={problem.row} disableGutters>
              <ListItemText primary={`Row ${problem.row}`} secondary={problem.problem} />
            </ListItem>
          ))}
        </List>
      )}
      {run.missing.length > 0 && (
        <Typography variant="body2">
          {`No longer in this export: ${run.missing.map((entry) => entry.reference).join(', ')}.`}
        </Typography>
      )}
    </Stack>
  );
}

export default function ExpectedImport({ api, onNotice }: { api: ExpectedApi; onNotice: (message: string) => void }) {
  const [sources, setSources] = useState<ImportSource[] | null>(null);
  const [runs, setRuns] = useState<ImportRun[]>([]);
  const [editing, setEditing] = useState<ImportSource | null | 'new'>(null);
  const [chosen, setChosen] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [full, setFull] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportRun | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      const [found, recent] = await Promise.all([api.sources(), api.imports()]);
      setSources(found.sources);
      setRuns(recent.imports);
      if (found.sources.length === 1) setChosen((current) => current || found.sources[0].name);
    } catch (failure) {
      setError(failure);
    }
  }, [api]);
  useEffect(() => { void load(); }, [load]);

  const upload = async () => {
    if (!file || !chosen) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api.importFile(chosen, file, full));
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Import an export of open work, such as open purchase orders, from another system. Importing the same file
        again adds nothing; a row that changed without a new revision waits for you; rows a full export no longer
        lists are shown to you and stay open.
      </Typography>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ mb: 3 }}>
        {(sources ?? []).map((source) => (
          <Card key={source.id} variant="outlined" sx={{ minWidth: 260 }}>
            <CardContent>
              <Typography variant="subtitle1">{source.name}</Typography>
              <Typography variant="body2" color="text.secondary">
                {`${source.format.toUpperCase()}; the reference is in ${source.mapping.reference}`}
                {source.mailbox ? `; rows with no mailbox go to ${source.mailbox}` : ''}
              </Typography>
              <Button size="small" sx={{ mt: 1 }} onClick={() => setEditing(source)}>Change</Button>
            </CardContent>
          </Card>
        ))}
        <Box><Button variant="outlined" onClick={() => setEditing('new')}>Add an import source</Button></Box>
      </Stack>
      {sources && sources.length > 0 && (
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} alignItems={{ md: 'center' }} sx={{ mb: 2 }}>
          <TextField select size="small" label="Import source" value={chosen} sx={{ minWidth: 240 }}
            onChange={(event) => setChosen(event.target.value)}>
            {sources.map((source) => <MenuItem key={source.id} value={source.name}>{source.name}</MenuItem>)}
          </TextField>
          <Button component="label" variant="outlined" startIcon={<UploadFileIcon />}>
            {file ? file.name : 'Choose the export file'}
            <input hidden type="file" accept=".csv,.json,text/csv,application/json"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          </Button>
          <FormControlLabel control={<Checkbox checked={full} onChange={(event) => setFull(event.target.checked)} />}
            label="This file lists all open work" />
          <Button variant="contained" disabled={!file || !chosen || busy} onClick={upload}>Import</Button>
        </Stack>
      )}
      {result && <Box sx={{ mb: 3 }}><RunResult run={result} /></Box>}
      <Typography variant="h6" component="h2">Recent imports</Typography>
      <List dense disablePadding>
        {runs.map((run) => (
          <ListItem key={run.id} disableGutters>
            <ListItemText primary={`${run.source ?? 'Import'}${run.file_name ? `, ${run.file_name}` : ''}, ${shortTime(run.completed_at ?? run.created_at)}`}
              secondary={run.summary} />
          </ListItem>
        ))}
        {runs.length === 0 && <ListItem disableGutters><ListItemText secondary="Nothing imported yet." /></ListItem>}
      </List>
      {editing && (
        <SourceDialog api={api} source={editing === 'new' ? null : editing} onClose={() => setEditing(null)}
          onSaved={(saved) => { setEditing(null); onNotice(`Saved ${saved.name}.`); setChosen(saved.name); void load(); }} />
      )}
    </Box>
  );
}
