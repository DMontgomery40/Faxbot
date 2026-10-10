// Numbers → line advice: when a line's copper closes (French communes from Orange's trajectory file, and carrier
// notice dates). Providers → trunk: country service rules (the UAE and Saudi Arabia), shown as not confirmed until
// you confirm them; nothing is blocked for them.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Link, MenuItem, Stack, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import { formatLocalDate } from '../../api/time';
import { Field, FormDialog } from '../access/AccessViews';
import LoadFailed, { saysFailure } from '../common/LoadFailed';
import { DeliveryError } from './shared';

// The government copy with only the five columns Faxbot reads (1.6 MB rather than 188 MB with the outlines).
const GOUV_SMALL = 'https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/fermeture-reseau-cuivre/exports/csv'
  + '?select=code_insee,nom_commune,fermeture_technique,fermeture_commerciale,lot';

export interface ClosureLine {
  number: string;
  account: string | null;
  site: string | null;
  state: 'passed' | 'soon' | 'later' | 'unscheduled' | null;
  sentences: string[];
  // A carrier's letter you can withdraw; a contract end comes from the line inventory and is changed there.
  notice?: { closes_on: string | null } | null;
}

export interface Closures {
  sites: Array<{ site: string; name: string; commune: string; state: string; sentence: string }>;
  lines: ClosureLine[];
  files: Array<{ source: 'orange' | 'gouv'; source_url: string | null; file_date: string | null; communes: number }>;
  sources: { orange: string; gouv: string; arcep: string };
}

function useRead<T>(client: AdminAPIClient, path: string) {
  const [value, setValue] = useState<T | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(async () => {
    try {
      setValue(await client.call<T>({ method: 'GET', path }));
      setFailed(false);
    } catch (failure) {
      setFailed(saysFailure(failure));
    }
  }, [client, path]);
  useEffect(() => { void load(); }, [load]);
  return { value, setValue, failed };
}

function ImportFile({ client, onDone }: { client: AdminAPIClient; onDone: (next: Closures, imported: number) => void }) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState<'gouv' | 'orange'>('gouv');
  const [fileDate, setFileDate] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await client.importClosureFile(file, { source, fileDate: fileDate || undefined });
      setOpen(false);
      onDone(result, result.imported);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>Import closure dates</Button>
      <FormDialog open={open} title="Import copper-closure dates" submitLabel="Import" busy={busy} canSubmit={Boolean(file)}
        onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Typography variant="body2" sx={{ mt: 1 }}>
          Orange's trajectory file lists each French commune's closure dates. Download the government copy (only the
          columns Faxbot reads) from <Link href={GOUV_SMALL} target="_blank" rel="noreferrer">data.economie.gouv.fr</Link>,
          or Orange's own file from its media library in a browser, saved with the same columns, and choose it here.
        </Typography>
        <TextField select fullWidth margin="normal" label="The file comes from" value={source}
          onChange={(event) => setSource(event.target.value as 'gouv' | 'orange')}>
          <MenuItem value="gouv">The government copy on data.gouv.fr</MenuItem>
          <MenuItem value="orange">Orange's own file</MenuItem>
        </TextField>
        <Button component="label" variant="outlined" size="small">
          {file ? file.name : 'Choose the CSV file'}
          <input hidden type="file" accept=".csv,text/csv" aria-label="Closure file"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        </Button>
        <Field label="The file's own date" type="date" value={fileDate} onChange={setFileDate} />
      </FormDialog>
    </>
  );
}

function AddNotice({ client, onDone }: { client: AdminAPIClient; onDone: (next: Closures) => void }) {
  const [open, setOpen] = useState(false);
  const [number, setNumber] = useState('');
  const [closes, setCloses] = useState('');
  const [carrier, setCarrier] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await client.call<Closures>({ method: 'PUT', path: `/routing/line-notices/${encodeURIComponent(number.trim())}`,
        body: { closes_on: closes, carrier: carrier.trim(), received_on: null, note: note.trim() } }));
      setOpen(false);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>Add a carrier's notice</Button>
      <FormDialog open={open} title="Add a carrier's notice" submitLabel="Save" busy={busy}
        canSubmit={Boolean(number.trim() && closes)} onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Typography variant="body2" sx={{ mt: 1 }}>
          Some carriers close copper lines by letter, with no public schedule (Canada, Israel and Italy, for example).
          Enter the date from the letter and Faxbot warns before it.
        </Typography>
        <Field label="Line's number" value={number} onChange={setNumber} placeholder="+14165550100" />
        <Field label="The line closes on" type="date" value={closes} onChange={setCloses} />
        <Field label="Carrier" value={carrier} onChange={setCarrier} />
        <Field label="Note" value={note} onChange={setNote} />
      </FormDialog>
    </>
  );
}

export function LineClosures({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const { value: closures, setValue, failed } = useRead<Closures>(client, '/routing/closures');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const remove = async (number: string) => {
    setError(null);
    try {
      setValue(await client.call<Closures>({ method: 'DELETE', path: `/routing/line-notices/${encodeURIComponent(number)}` }));
    } catch (failure) {
      setError(failure);
    }
  };
  if (failed) return <LoadFailed testId="closures-unread" text="Line closures could not be loaded. Try again." />;
  // An answer without the lists (an older server) shows nothing rather than failing the page.
  if (!closures || !Array.isArray(closures.files) || !Array.isArray(closures.sites) || !Array.isArray(closures.lines)) return null;
  return (
    <Box component="section" mt={4} data-testid="line-closures">
      <Typography variant="h6" component="h2">When lines close</Typography>
      <Typography variant="body2" color="text.secondary" mb={1}>
        France closes its copper network, and the phone lines on it, commune by commune. Give each French site its
        commune under the organization's sites, and import Orange's dates.
      </Typography>
      {message && <Alert severity="success" sx={{ mb: 1 }} onClose={() => setMessage(null)}>{message}</Alert>}
      <DeliveryError error={error} />
      {closures.files.map((item) => (
        <Typography key={item.source} variant="body2">
          {item.source === 'orange' ? "Orange's file" : 'Government copy'}: {item.communes.toLocaleString()} communes
          {item.file_date ? `, file of ${formatLocalDate(item.file_date)}` : ''}
          {item.source_url ? <> · <Link href={item.source_url} target="_blank" rel="noreferrer">source</Link></> : null}
        </Typography>
      ))}
      {closures.files.length === 0 && (
        <Typography variant="body2">
          No closure file yet. <Link href={closures.sources.arcep} target="_blank" rel="noreferrer">ARCEP's page</Link> explains
          the calendar.
        </Typography>
      )}
      {closures.sites.map((site) => (
        <Typography key={site.site} variant="body2"><strong>{site.name}:</strong> {site.sentence}</Typography>
      ))}
      {closures.lines.map((line) => (
        <Alert key={line.number} severity={line.state === 'later' ? 'info' : 'warning'} sx={{ mt: 1 }}
          action={canWrite && !line.site && line.notice !== null ? (
            <Button color="inherit" size="small" onClick={() => void remove(line.number)}
              aria-label={`Remove the notice for ${line.number}`}>Remove</Button>) : undefined}>
          <strong>{line.number}</strong>{line.account ? ` (${line.account})` : ''}: {line.sentences.join(' ')}
        </Alert>
      ))}
      {canWrite && (
        <Stack direction="row" spacing={1} mt={1}>
          <ImportFile client={client} onDone={(next, imported) => {
            setValue(next);
            setMessage(`Imported ${imported.toLocaleString()} communes.`);
          }} />
          <AddNotice client={client} onDone={setValue} />
        </Stack>
      )}
    </Box>
  );
}

interface CountryRulesView {
  accounts: Array<{ account: string; label: string; country: string; confirmed: boolean; sentence: string }>;
  countries: Array<{ country: string; name: string; regulator: string; sentence: string;
    sources: Array<{ label: string; url: string }> }>;
}

function ConfirmCountry({ client, account, country, label, onDone }: {
  client: AdminAPIClient; account: string; country: string; label: string; onDone: (next: CountryRulesView) => void;
}) {
  const [open, setOpen] = useState(false);
  const [evidence, setEvidence] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await client.call<CountryRulesView>({ method: 'POST', path: '/routing/country-rules/confirm',
        body: { account, country, evidence: evidence.trim(), evidence_url: null } }));
      setOpen(false);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" onClick={() => { setError(null); setOpen(true); }} aria-label={`Confirm ${label}`}>Confirm</Button>
      <FormDialog open={open} title={`Confirm ${label}`} submitLabel="Confirm" busy={busy} canSubmit={Boolean(evidence.trim())}
        onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Field label="How you know" value={evidence} onChange={setEvidence} multiline
          helperText="For example, your provider's licence or your contract with a licensed carrier." />
      </FormDialog>
    </>
  );
}

export function CountryRules({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const { value: rules, setValue, failed } = useRead<CountryRulesView>(client, '/routing/country-rules');
  if (failed) return <LoadFailed testId="country-rules-unread" text="Country rules could not be loaded. Try again." />;
  if (!rules || !Array.isArray(rules.accounts) || !Array.isArray(rules.countries) || rules.accounts.length === 0) return null;
  const countries = new Set(rules.accounts.map((item) => item.country));
  return (
    <Box component="section" mt={3} data-testid="country-rules">
      <Typography variant="h6" component="h2">Country rules</Typography>
      {rules.countries.filter((item) => countries.has(item.country)).map((item) => (
        <Typography key={item.country} variant="body2" mb={1}>
          {item.sentence}{' '}
          {item.sources.map((source) => (
            <Link key={source.url} href={source.url} target="_blank" rel="noreferrer" sx={{ mr: 1 }}>{source.label}</Link>
          ))}
        </Typography>
      ))}
      {rules.accounts.map((item) => (
        <Alert key={`${item.account}-${item.country}`} severity={item.confirmed ? 'success' : 'info'} sx={{ mt: 1 }}
          action={canWrite && !item.confirmed ? (
            <ConfirmCountry client={client} account={item.account} country={item.country} label={item.label}
              onDone={setValue} />) : undefined}>
          {item.sentence}
        </Alert>
      ))}
    </Box>
  );
}
