// Costs → Recommendations → Fax server renewal (N20, N24): the channels another fax server really needed, from its
// call records, and one page for its renewal. Advice only: nothing changes a licence or contacts a vendor.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Link, MenuItem, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import { formatLocalDate } from '../../api/time';
import { Field, FormDialog } from '../access/AccessViews';
import { DeliveryError } from './shared';

interface ChannelReport {
  calls: number;
  peak: number;
  p99?: number;
  sentence: string;
  profile?: Array<{ hour: number; p99: number; peak: number }>;
}

interface ChannelSystem {
  system: string;
  own: boolean;
  source: string;
  report: ChannelReport;
  imports: Array<{ id: string; format_label: string; file_name: string | null; calls: number }>;
}

export interface ChannelsView {
  systems: ChannelSystem[];
  sentence: string | null;
  note: string;
}

interface Reference { id: string; sentence: string; source: string; label: string; read_on: string }

interface RenewalPage {
  system: string;
  sentences: string[];
  renewal: { state: 'early' | 'review' | 'passed'; source_url: string | null } | null;
  left: { numbers: Array<{ number: string; display: string; user_name: string | null; user_email: string | null }> } | null;
  reference: Reference[];
}

export interface RenewalsView {
  pages: RenewalPage[];
  // The channel report, read with the pages so each system's calls are read once.
  channels: ChannelsView;
  sentence: string | null;
  help: { routes: string };
  note: string;
}

const FORMATS = [
  { value: '', label: 'Let Faxbot recognise it' },
  { value: 'rightfax', label: 'RightFax DocTransport audit log (level 3 or 4)' },
  { value: 'asterisk', label: "Asterisk call records (Master.csv)" },
  { value: 'cucm', label: 'Cisco Unified CM call records' },
  { value: 'faxmaker', label: 'GFI FaxMaker activity export' },
  { value: 'csv', label: 'CSV: start, end or duration, direction, channel, number' },
];

function usable<T>(value: unknown, key: string): value is T {
  return Boolean(value && Array.isArray((value as Record<string, unknown>)[key]));
}

function ImportCalls({ client, onDone }: { client: AdminAPIClient; onDone: (next: RenewalsView, message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [system, setSystem] = useState('');
  const [format, setFormat] = useState('');
  const [licensed, setLicensed] = useState('');
  const [zone, setZone] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const next = await client.importChannelCalls<RenewalsView & { imported: number; skipped_count: number }>(
        file, { system: system.trim(), source_format: format || undefined,
          licensed: licensed.trim() || undefined, time_zone: zone.trim() || undefined });
      setOpen(false);
      onDone(next, `Imported ${next.imported.toLocaleString()} calls.`
        + (next.skipped_count ? ` ${next.skipped_count.toLocaleString()} ${next.skipped_count === 1 ? 'line was' : 'lines were'} not read.` : ''));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>Import call records</Button>
      <FormDialog open={open} title="Import a fax server's call records" submitLabel="Import" busy={busy}
        canSubmit={Boolean(file && system.trim())} onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Field label="Fax server" value={system} onChange={setSystem} placeholder="RightFax at HQ" />
        <Button component="label" variant="outlined" size="small">
          {file ? file.name : 'Choose the call records file'}
          <input hidden type="file" aria-label="Call records file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        </Button>
        <TextField select fullWidth margin="normal" label="The file is" value={format} onChange={(event) => setFormat(event.target.value)}>
          {FORMATS.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}
        </TextField>
        <Field label="Channels it is licensed for" value={licensed} onChange={setLicensed} type="number" />
        <Field label="Time zone of the times in the file" value={zone} onChange={setZone}
          helperText="Such as America/Denver. Leave empty to use this server's time zone." />
      </FormDialog>
    </>
  );
}

function EnterRenewal({ client, onDone }: { client: AdminAPIClient; onDone: (next: RenewalsView) => void }) {
  const [open, setOpen] = useState(false);
  const [system, setSystem] = useState('');
  const [product, setProduct] = useState('');
  const [renews, setRenews] = useState('');
  const [amount, setAmount] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [channels, setChannels] = useState('');
  const [source, setSource] = useState('');
  const [parallel, setParallel] = useState('');
  const [since, setSince] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await client.call<RenewalsView>({ method: 'PUT', path: '/routing/renewals', body: {
        system: system.trim(), product: product.trim(), renews_on: renews, amount: amount.trim(), currency: currency.trim(),
        licensed_channels: channels.trim() ? Number(channels) : null, source_url: source.trim() || null,
        parallel_numbers: parallel.split(/[\s,]+/).filter(Boolean), parallel_since: since || null } }));
      setOpen(false);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>Enter a renewal</Button>
      <FormDialog open={open} title="Enter a fax server's renewal" submitLabel="Save" busy={busy}
        canSubmit={Boolean(system.trim() && renews && amount.trim())} onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Field label="Fax server" value={system} onChange={setSystem} placeholder="RightFax at HQ" />
        <Field label="Product and version" value={product} onChange={setProduct} placeholder="RightFax 22.2" />
        <Field label="Renews on" type="date" value={renews} onChange={setRenews} />
        <Field label="Renewal amount" value={amount} onChange={setAmount} placeholder="26756.71" />
        <Field label="Currency" value={currency} onChange={setCurrency} />
        <Field label="Channels it licenses" value={channels} onChange={setChannels} type="number" />
        <Field label="Where the amount comes from" value={source} onChange={setSource}
          helperText="A link to the quote or contract, if you have one." />
        <Field label="Numbers Faxbot receives on beside it" value={parallel} onChange={setParallel}
          helperText="Separate numbers with commas." />
        <Field label="The parallel run began on" type="date" value={since} onChange={setSince} />
      </FormDialog>
    </>
  );
}

function ImportRoutes({ client, help, onDone }: { client: AdminAPIClient; help: string; onDone: (next: RenewalsView, message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [system, setSystem] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const next = await client.importRenewalRoutes<RenewalsView & { imported: number }>(file,
        { system: system.trim() });
      setOpen(false);
      onDone(next, `Imported ${next.imported.toLocaleString()} numbers.`);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>Import number routing</Button>
      <FormDialog open={open} title="Import a fax server's number routing" submitLabel="Import" busy={busy}
        canSubmit={Boolean(file && system.trim())} onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Typography variant="body2" sx={{ mt: 1, mb: 1 }}>{help}</Typography>
        <Field label="Fax server" value={system} onChange={setSystem} placeholder="RightFax at HQ" />
        <Button component="label" variant="outlined" size="small">
          {file ? file.name : 'Choose the routing file'}
          <input hidden type="file" accept=".csv,.xlsx,text/csv" aria-label="Routing file"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        </Button>
      </FormDialog>
    </>
  );
}

export default function FaxServerRenewal({ client, canWrite, onCount }: {
  client: AdminAPIClient; canWrite: boolean; onCount?: (count: number | null) => void;
}) {
  const [renewals, setRenewals] = useState<RenewalsView | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(async () => {
    try {
      const pages = await client.call<unknown>({ method: 'GET', path: '/routing/renewals' });
      setRenewals(usable<RenewalsView>(pages, 'pages') && usable<ChannelsView>((pages as RenewalsView).channels, 'systems')
        ? pages : null);
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    }
  }, [client, onCount]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (renewals) onCount?.(renewals.pages.length + renewals.channels.systems.filter((item) => !item.own).length);
  }, [renewals, onCount]);
  const write = async (request: { method: string; path: string; body?: unknown }, done: string) => {
    setError(null);
    try {
      await client.call<unknown>(request);
      await load();
      setMessage(done);
    } catch (failure) {
      setError(failure);
    }
  };
  if (!renewals) return error ? <DeliveryError error={error} /> : null;
  const channels = renewals.channels;
  return (
    <Paper variant="outlined" sx={{ p: 3, borderRadius: 2 }} data-testid="fax-server-renewal">
      <Typography variant="h6" component="h2">Fax server renewal</Typography>
      {message && <Alert severity="success" sx={{ my: 1 }} onClose={() => setMessage(null)}>{message}</Alert>}
      <DeliveryError error={error} />
      {renewals.sentence && <Typography variant="body2" mt={1}>{renewals.sentence}</Typography>}
      {renewals.pages.map((page) => (
        <Box key={page.system} mt={2} data-testid="renewal-page">
          <Typography variant="subtitle1" fontWeight={600}>{page.system}</Typography>
          {page.sentences.map((sentence, index) => (
            <Typography key={index} variant="body2" color={index === 0 && page.renewal?.state === 'review' ? 'warning.main' : undefined}>
              {sentence}
            </Typography>
          ))}
          {page.renewal?.source_url && <Link href={page.renewal.source_url} target="_blank" rel="noreferrer" variant="body2">The quote</Link>}
          {canWrite && page.renewal && <Button size="small" aria-label={`Withdraw the ${page.system} renewal`}
            onClick={() => void write({ method: 'POST', path: '/routing/renewals/remove', body: { system: page.system } },
              'Renewal withdrawn.')}>Withdraw</Button>}
          {page.left && page.left.numbers.length > 0 && (
            <Typography variant="body2" color="text.secondary" mt={1}>
              Still to move: {page.left.numbers.slice(0, 20).map((item) => item.display
                + (item.user_name ? ` (${item.user_name})` : '')).join(', ')}
              {page.left.numbers.length > 20 ? ` and ${(page.left.numbers.length - 20).toLocaleString()} more` : ''}.
            </Typography>
          )}
          {page.reference.map((item) => (
            <Typography key={item.id} variant="body2" color="text.secondary" mt={0.5}>
              For comparison: {item.sentence} <Link href={item.source} target="_blank" rel="noreferrer">{item.label}</Link>,
              read {formatLocalDate(item.read_on)}.
            </Typography>
          ))}
        </Box>
      ))}
      <Typography variant="subtitle1" fontWeight={600} mt={2}>Channels at their busiest</Typography>
      {channels.sentence && <Typography variant="body2">{channels.sentence}</Typography>}
      {channels.systems.map((item) => {
        const busy = (item.report.profile ?? []).filter((hour) => hour.peak > 0);
        return (
          <Box key={item.system} mt={1}>
            <Typography variant="body2"><strong>{item.system}</strong> ({item.source}): {item.report.sentence}</Typography>
            {busy.length > 0 && (
              <Table size="small" sx={{ maxWidth: 420 }} aria-label={`${item.system} by hour of the day`}>
                <TableHead><TableRow><TableCell>Hour</TableCell><TableCell>99 hours in 100</TableCell><TableCell>Most at once</TableCell></TableRow></TableHead>
                <TableBody>
                  {busy.map((hour) => (
                    <TableRow key={hour.hour}><TableCell>{`${String(hour.hour).padStart(2, '0')}:00`}</TableCell>
                      <TableCell>{hour.p99}</TableCell><TableCell>{hour.peak}</TableCell></TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {item.imports.map((found) => (
              <Typography key={found.id} variant="body2" color="text.secondary">
                {found.format_label}: {found.file_name || 'a file'}, {found.calls.toLocaleString()} calls
                {canWrite && <Button size="small" onClick={() => void write({ method: 'DELETE',
                  path: `/routing/channels/imports/${encodeURIComponent(found.id)}` }, 'File left out of the report.')}
                  aria-label={`Leave ${found.file_name || 'this file'} out`}>Leave out</Button>}
              </Typography>
            ))}
          </Box>
        );
      })}
      {canWrite && (
        <Stack direction="row" spacing={1} mt={2} flexWrap="wrap" useFlexGap>
          <EnterRenewal client={client} onDone={(next) => { setRenewals(next); setMessage('Renewal saved.'); }} />
          <ImportCalls client={client} onDone={(next, text) => { setRenewals(next); setMessage(text); }} />
          <ImportRoutes client={client} help={renewals.help.routes} onDone={(next, text) => { setRenewals(next); setMessage(text); }} />
        </Stack>
      )}
      <Typography variant="body2" color="text.secondary" mt={1}>{renewals.note}</Typography>
    </Paper>
  );
}
