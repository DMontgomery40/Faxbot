// Faxes → Expected → Outages: while a system that expected faxes come from is down, record what was
// done by fax, email or phone against each item's original ID. After it is back, the next export is
// sorted into three lists. Faxbot never sends or submits anything here.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, FormControlLabel, List, ListItem, ListItemText, MenuItem, Paper, Stack, Table,
  TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import type { ImportSource, Outage, OutageAction, ReconciliationEntry } from '../../api/expectedTypes';
import { DeliveryError } from '../delivery/shared';
import { shortTime } from '../work/text';
import type { ExpectedApi } from './expectedApi';

const CHANNELS: Array<[OutageAction['channel'], string]> = [
  ['fax', 'By fax'], ['email', 'By email'], ['phone', 'By phone'], ['other', 'Another way'],
];

function ListSection({ title, entries, empty }: { title: string; entries: ReconciliationEntry[]; empty: string }) {
  return (
    <Box>
      <Typography variant="subtitle1">{`${title} (${entries.length})`}</Typography>
      <List dense disablePadding>
        {entries.map((entry) => (
          <ListItem key={`${entry.operation_id}-${entry.revision ?? ''}`} disableGutters>
            <ListItemText primary={`${entry.reference || entry.operation_id}${entry.mailbox ? `, ${entry.mailbox}` : ''}`}
              secondary={entry.text} />
          </ListItem>
        ))}
        {entries.length === 0 && <ListItem disableGutters><ListItemText secondary={empty} /></ListItem>}
      </List>
    </Box>
  );
}

function RecordForm({ api, outage, onRecorded }: { api: ExpectedApi; outage: Outage; onRecorded: (next: Outage) => void }) {
  const blank = { operation_id: '', revision: '', reference: '', action: '', channel: 'fax' as OutageAction['channel'],
    uncertain: false, fax_job_id: '', evidence_note: '' };
  const [form, setForm] = useState(blank);
  const [error, setError] = useState<unknown>(null);
  const set = (name: keyof typeof blank) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setForm((current) => ({ ...current, [name]: name === 'uncertain' ? event.target.checked : event.target.value }));
  const save = async () => {
    setError(null);
    try {
      onRecorded(await api.recordAction(outage.code, {
        operation_id: form.operation_id.trim(), action: form.action.trim(), channel: form.channel,
        outcome: form.uncertain ? 'uncertain' : 'done', revision: form.revision.trim() || undefined,
        reference: form.reference.trim() || undefined, fax_job_id: form.fax_job_id.trim() || undefined,
        evidence_note: form.evidence_note.trim() || undefined,
      }));
      setForm(blank);
    } catch (failure) {
      setError(failure);
    }
  };
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>Record what was done</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Stack spacing={2}>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
          <TextField fullWidth label="Its ID in the system" value={form.operation_id} onChange={set('operation_id')} required
            helperText="The original ID, as the system's export will show it." />
          <TextField label="Revision" value={form.revision} onChange={set('revision')} />
          <TextField fullWidth label="Reference" value={form.reference} onChange={set('reference')} />
        </Stack>
        <TextField label="What was done" value={form.action} onChange={set('action')} required
          helperText="Such as Order faxed to Acme Supply." />
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
          <TextField select label="How" value={form.channel} onChange={set('channel')} sx={{ minWidth: 160 }}>
            {CHANNELS.map(([key, label]) => <MenuItem key={key} value={key}>{label}</MenuItem>)}
          </TextField>
          <TextField fullWidth label="Sent fax ID" value={form.fax_job_id} onChange={set('fax_job_id')}
            helperText="When it went as a fax from Faxbot: copy its ID from Sent." />
          <TextField fullWidth label="Where the evidence is" value={form.evidence_note} onChange={set('evidence_note')} />
        </Stack>
        <FormControlLabel control={<Checkbox checked={form.uncertain} onChange={set('uncertain')} />}
          label="It may not have gone through" />
        <Box>
          <Button variant="contained" disabled={!form.operation_id.trim() || !form.action.trim()} onClick={save}>Record it</Button>
        </Box>
      </Stack>
    </Paper>
  );
}

export default function ExpectedOutages({ api, onNotice }: { api: ExpectedApi; onNotice: (message: string) => void }) {
  const [outages, setOutages] = useState<Outage[] | null>(null);
  const [sources, setSources] = useState<ImportSource[]>([]);
  const [chosen, setChosen] = useState<Outage | null>(null);
  const [source, setSource] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      const [found, saved] = await Promise.all([api.outages(), api.sources()]);
      setOutages(found.outages);
      setSources(saved.sources);
    } catch (failure) {
      setError(failure);
    }
  }, [api]);
  useEffect(() => { void load(); }, [load]);

  const open = async (code: string) => {
    try { setChosen(await api.outage(code)); } catch (failure) { setError(failure); }
  };
  const act = async (work: () => Promise<Outage>, message: string) => {
    setError(null);
    try {
      setChosen(await work());
      onNotice(message);
      await load();
    } catch (failure) {
      setError(failure);
    }
  };

  return (
    <Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        When a system that expected faxes come from is down, mark it down here and record what you do by fax or
        email meanwhile. When it is back, import its next export: Faxbot sorts each item into already done, new and
        held for you. Faxbot never sends or submits anything for you here.
      </Typography>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} alignItems={{ md: 'center' }} sx={{ mb: 3 }}>
        <TextField select size="small" label="System that is down" value={source} sx={{ minWidth: 240 }}
          onChange={(event) => setSource(event.target.value)}
          helperText={sources.length ? ' ' : 'Add an import source first, under Import.'}>
          {sources.map((entry) => <MenuItem key={entry.id} value={entry.name}>{entry.name}</MenuItem>)}
        </TextField>
        <TextField size="small" label="Note" value={note} onChange={(event) => setNote(event.target.value)} helperText=" " />
        <Button variant="contained" disabled={!source}
          onClick={() => act(() => api.startOutage(source, note.trim() || undefined), `${source} is marked down.`)}>
          Mark it down
        </Button>
      </Stack>
      <Table size="small" sx={{ mb: 3 }}>
        <TableHead><TableRow><TableCell>System</TableCell><TableCell>When</TableCell><TableCell /></TableRow></TableHead>
        <TableBody>
          {(outages ?? []).map((outage) => (
            <TableRow key={outage.id} selected={chosen?.id === outage.id}>
              <TableCell>{outage.source}</TableCell>
              <TableCell>{outage.open ? `Down since ${shortTime(outage.started_at)}.`
                : `Down from ${shortTime(outage.started_at)} to ${shortTime(outage.ended_at)}.`}</TableCell>
              <TableCell align="right"><Button size="small" onClick={() => open(outage.code)}>Open</Button></TableCell>
            </TableRow>
          ))}
          {outages && outages.length === 0 && <TableRow><TableCell colSpan={3}>No outages recorded.</TableCell></TableRow>}
        </TableBody>
      </Table>
      {chosen && (
        <Stack spacing={2}>
          <Typography variant="h6" component="h2">
            {chosen.open ? `${chosen.source} has been down since ${shortTime(chosen.started_at)}`
              : `${chosen.source} was down from ${shortTime(chosen.started_at)} to ${shortTime(chosen.ended_at)}`}
          </Typography>
          <Stack direction="row" spacing={1}>
            {chosen.open && (
              <Button variant="outlined" onClick={() => act(() => api.endOutage(chosen.code, chosen.version),
                `${chosen.source} is marked back. Import its next export to sort the work.`)}>Mark it back</Button>
            )}
            {!chosen.open && (
              <Button variant="outlined" onClick={() => act(() => api.reconcile(chosen.code), 'Sorted again.')}>
                Sort the latest export again
              </Button>
            )}
          </Stack>
          {chosen.open && <RecordForm api={api} outage={chosen} onRecorded={(next) => { setChosen(next); onNotice('Recorded.'); }} />}
          <Typography variant="subtitle1">{`Done during the outage (${chosen.actions?.length ?? 0})`}</Typography>
          <List dense disablePadding>
            {(chosen.actions ?? []).map((action, index) => (
              <ListItem key={index} disableGutters>
                <ListItemText primary={`${action.reference || action.operation_id}: ${action.action}`}
                  secondary={`${CHANNELS.find(([key]) => key === action.channel)?.[1]}, ${shortTime(action.occurred_at)}`
                    + (action.outcome === 'uncertain' ? '; it may not have gone through.' : '.')} />
              </ListItem>
            ))}
          </List>
          {chosen.reconciliation ? (
            <Stack spacing={2}>
              <Alert severity={chosen.reconciliation.unresolved.length ? 'warning' : 'success'}>{chosen.reconciliation.summary}</Alert>
              <ListSection title="Already done: record it in the system, do not submit it again"
                entries={chosen.reconciliation.already_done} empty="None." />
              <ListSection title="New: submit it normally" entries={chosen.reconciliation.new} empty="None." />
              <ListSection title="Held for you to decide" entries={chosen.reconciliation.unresolved} empty="None." />
            </Stack>
          ) : !chosen.open && (
            <Alert severity="info">Import the system&apos;s next export to sort its work into the three lists.</Alert>
          )}
        </Stack>
      )}
    </Box>
  );
}
