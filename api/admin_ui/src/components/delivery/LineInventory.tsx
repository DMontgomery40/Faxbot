// Numbers → Advice and moves: your line inventory, matched to carriers' lists of discontinued or grandfathered
// areas (AT&T's workbook as published) and to each line's contract end date (N19). Advice only.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Link, MenuItem, Stack, TextField, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import { formatLocalDate, formatServerTime } from '../../api/time';
import { Field, FormDialog } from '../access/AccessViews';
import LoadFailed, { saysFailure } from '../common/LoadFailed';
import { DeliveryError } from './shared';

type DateState = 'passed' | 'soon' | 'later';

export interface InventoryDate {
  kind: 'letter' | 'contract_end' | 'carrier_list';
  date: string;
  state: DateState;
  sentence: string;
  source: string;
  source_url: string | null;
}

export interface InventoryLine {
  number: string;
  display: string;
  carrier: string | null;
  wire_center: string | null;
  use_label: string;
  monthly: string | null;
  account: string | null;
  match: { state: 'listed' | 'possible' | 'no_wire_center'; sentence: string } | null;
  dates: InventoryDate[];
}

export interface CarrierList {
  carrier: string;
  kind: string;
  label: string;
  source_url: string | null;
  file_date: string | null;
  areas: number;
  wire_centers: number;
  first: string | null;
  last: string | null;
}

export interface LineInventoryView {
  sentence: string;
  lines: InventoryLine[];
  inventory: { file_name: string | null; lines: number; imported_by: string | null; imported_at: string } | null;
  lists: CarrierList[];
  keyed: string;
  note: string;
  help: { inventory: string; list: string };
  sources: { att_workbook: string };
}

type Imported = LineInventoryView & { imported: number; skipped: string[]; skipped_count: number };

function usable(value: unknown): value is LineInventoryView {
  const view = value as LineInventoryView | null;
  return Boolean(view && Array.isArray(view.lines) && Array.isArray(view.lists) && view.help && view.sources);
}

function ImportDialog({ client, title, button, path, help, kind, onDone }: {
  client: AdminAPIClient; title: string; button: string; path: string; help: string; kind: 'inventory' | 'list';
  onDone: (next: Imported) => void;
}) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [dateOrder, setDateOrder] = useState('mdy');
  const [carrier, setCarrier] = useState('');
  const [listKind, setListKind] = useState('discontinued');
  const [fileDate, setFileDate] = useState('');
  const [sourceUrl, setSourceUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const fields = kind === 'inventory' ? { date_order: dateOrder } : {
        date_order: dateOrder, carrier: carrier.trim() || undefined,
        kind: carrier.trim() ? listKind : undefined, file_date: fileDate || undefined,
        source_url: sourceUrl.trim() || undefined };
      onDone(await client.postFile<Imported>(path, file, fields));
      setOpen(false);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>{button}</Button>
      <FormDialog open={open} title={title} submitLabel="Import" busy={busy} canSubmit={Boolean(file)}
        onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Typography variant="body2" sx={{ mt: 1, mb: 1 }}>{help}</Typography>
        <Button component="label" variant="outlined" size="small">
          {file ? file.name : 'Choose the file (CSV or Excel)'}
          <input hidden type="file" accept=".csv,.xlsx,text/csv" aria-label={kind === 'inventory' ? 'Inventory file' : 'Carrier list file'}
            onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        </Button>
        <TextField select fullWidth margin="normal" label="Dates in the file" value={dateOrder}
          onChange={(event) => setDateOrder(event.target.value)}>
          <MenuItem value="mdy">Month first: 11/4/2026 is 4 November</MenuItem>
          <MenuItem value="dmy">Day first: 4/11/2026 is 4 November</MenuItem>
        </TextField>
        {kind === 'list' && (
          <>
            <Field label="Carrier" value={carrier} onChange={setCarrier}
              helperText="Leave empty for AT&T's workbook or a file with a carrier column." />
            {carrier.trim() && (
              <TextField select fullWidth margin="normal" label="The list says these areas are" value={listKind}
                onChange={(event) => setListKind(event.target.value)}>
                <MenuItem value="discontinued">Discontinued</MenuItem>
                <MenuItem value="grandfathered">Grandfathered (no new orders or changes)</MenuItem>
              </TextField>
            )}
            <Field label="The list's own date" type="date" value={fileDate} onChange={setFileDate} />
            <Field label="Where you downloaded it" value={sourceUrl} onChange={setSourceUrl}
              helperText="Leave empty for AT&T's workbook; Faxbot knows its address." />
          </>
        )}
      </FormDialog>
    </>
  );
}

function lineSentences(line: InventoryLine): string[] {
  const sentences = line.dates.map((item) => item.sentence);
  if (line.match && line.match.state !== 'listed') sentences.push(line.match.sentence);
  return sentences;
}

export default function LineInventory({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [view, setView] = useState<LineInventoryView | null>(null);
  const [failed, setFailed] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [skipped, setSkipped] = useState<string[]>([]);
  const load = useCallback(async () => {
    try {
      const found = await client.call<unknown>({ method: 'GET', path: '/routing/line-inventory' });
      setView(usable(found) ? found : null);
      setFailed(false);
    } catch (failure) {
      setFailed(saysFailure(failure));
    }
  }, [client]);
  useEffect(() => { void load(); }, [load]);
  if (failed) return <LoadFailed testId="line-inventory-unread" text="Your line inventory could not be loaded. Try again." />;
  // An answer without the lists (an older server) shows nothing rather than failing the page.
  if (!view) return null;
  const done = (noun: string) => (next: Imported) => {
    setView(next);
    setSkipped(next.skipped ?? []);
    setMessage(`Imported ${next.imported.toLocaleString()} ${noun}.`
      + (next.skipped_count ? ` ${next.skipped_count.toLocaleString()} ${next.skipped_count === 1 ? 'row was' : 'rows were'} not read.` : ''));
  };
  const shown = view.lines.filter((line) => lineSentences(line).length > 0);
  const quiet = view.lines.length - shown.length;
  return (
    <Box component="section" mt={4} data-testid="line-inventory">
      <Typography variant="h6" component="h2">Line inventory and carrier dates</Typography>
      <Typography variant="body2" mb={1}>{view.sentence}</Typography>
      {message && <Alert severity="success" sx={{ mb: 1 }} onClose={() => setMessage(null)}>{message}</Alert>}
      {skipped.map((reason) => <Typography key={reason} variant="body2" color="text.secondary">{reason}</Typography>)}
      {view.inventory && (
        <Typography variant="body2">
          {view.inventory.lines.toLocaleString()} lines from {view.inventory.file_name || 'your file'}, imported
          {view.inventory.imported_by ? ` by ${view.inventory.imported_by}` : ''} on {formatServerTime(view.inventory.imported_at)}.
        </Typography>
      )}
      {view.lists.map((item) => (
        <Typography key={`${item.carrier}-${item.kind}`} variant="body2">
          {item.label}: {item.areas.toLocaleString()} areas in {item.wire_centers.toLocaleString()} wire centers, dated
          {' '}{formatLocalDate(item.first)} to {formatLocalDate(item.last)}
          {item.file_date ? `; file of ${formatLocalDate(item.file_date)}` : ''}
          {item.source_url ? <> · <Link href={item.source_url} target="_blank" rel="noreferrer">source</Link></> : null}
        </Typography>
      ))}
      {view.lists.length === 0 && (
        <Typography variant="body2">
          No carrier list yet. Download <Link href={view.sources.att_workbook} target="_blank" rel="noreferrer">AT&T's
          Discontinued TDM Service Areas workbook</Link> and import it as it is.
        </Typography>
      )}
      <Typography variant="body2" color="text.secondary" mt={1}>{view.keyed}</Typography>
      {shown.map((line) => {
        const state = line.dates[0]?.state;
        return (
          <Alert key={line.number} severity={state === 'passed' || state === 'soon' ? 'warning' : 'info'} sx={{ mt: 1 }}>
            <strong>{line.display}</strong> ({line.use_label}{line.carrier ? `, ${line.carrier}` : ''}
            {line.wire_center ? `, wire center ${line.wire_center}` : ''}
            {line.account ? `, in Faxbot on ${line.account}` : ''}
            {line.monthly ? `, ${line.monthly} a month` : ''}): {lineSentences(line).join(' ')}
          </Alert>
        );
      })}
      {view.lines.length > 0 && quiet > 0 && (
        <Typography variant="body2" mt={1}>
          {quiet.toLocaleString()} other {quiet === 1 ? 'line has' : 'lines have'} no date and no match.
        </Typography>
      )}
      {canWrite && (
        <Stack direction="row" spacing={1} mt={1}>
          <ImportDialog client={client} kind="inventory" title="Import your line inventory" button="Import line inventory"
            path="/routing/line-inventory/files" help={view.help.inventory} onDone={done('lines')} />
          <ImportDialog client={client} kind="list" title="Import a carrier's list" button="Import carrier list"
            path="/routing/carrier-lists/files" help={view.help.list} onDone={done('areas')} />
        </Stack>
      )}
      <Typography variant="body2" color="text.secondary" mt={1}>{view.note}</Typography>
    </Box>
  );
}
