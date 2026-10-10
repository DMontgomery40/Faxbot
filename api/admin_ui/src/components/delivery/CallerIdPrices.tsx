// Savings & optimization → Prices & plans: prices by the caller ID a call shows. Some carriers charge much less for a call to a
// country when the caller ID comes from that country or the EEA. Faxbot uses a lower price only for a caller ID
// you confirmed on that account, and never changes the caller ID an account shows.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, FormControlLabel, Link, MenuItem, Paper, Stack, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { CallerIdPrices as Prices, CallerIdQuote, SendingCaller } from '../../api/countriesTypes';
import { formatLocalDate } from '../../api/time';
import { Field, FormDialog } from '../access/AccessViews';
import LoadFailed, { saysFailure } from '../common/LoadFailed';
import { DeliveryError } from './shared';

const FOR_TYPE: Record<string, string> = {
  local: 'for local caller IDs', eea: 'for EEA caller IDs', non_surcharged: 'for listed caller IDs',
  surcharged: 'for any other caller ID',
};

function status(caller: SendingCaller): string {
  if (!caller.priced_by_caller_id) return 'Not needed';
  if (!caller.caller_id) return 'No caller ID';
  if (!caller.eligibility) return 'Not confirmed';
  if (caller.eligibility.state === 'withdrawn') return 'Withdrawn';
  return caller.eligibility.bought_here ? 'Confirmed, bought on this account' : 'Confirmed';
}

function ImportDeck({ client, routes, layouts, onDone }: {
  client: AdminAPIClient;
  routes: string[];
  layouts: Prices['layouts'];
  onDone: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [route, setRoute] = useState(routes[0] ?? '');
  const [file, setFile] = useState<File | null>(null);
  const [layout, setLayout] = useState('');
  const [source, setSource] = useState('');
  const [published, setPublished] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await client.importCallerIdDeck(route.trim(), file, {
        deckFormat: layout || undefined, sourceUrl: source.trim() || undefined, publishedOn: published || undefined });
      setOpen(false);
      setFile(null);
      const skipped = result.skipped_count ? ` ${result.skipped_count} lines could not be read.` : '';
      onDone(`Imported ${result.deck.rows.toLocaleString()} prices for ${result.deck.route}. ${result.terms}${skipped}`);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>Import a rate deck</Button>
      <FormDialog open={open} title="Import a rate deck priced by caller ID" submitLabel="Import" busy={busy}
        canSubmit={Boolean(file && route.trim())} onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          Twilio's voice price file is read as it is. {layouts.telnyx} {layouts.faxbot}
        </Typography>
        <TextField fullWidth margin="normal" label="Prices for" value={route} onChange={(event) => setRoute(event.target.value)}
          helperText="The sending card or account the deck belongs to, such as sip-telnyx." select={routes.length > 0}>
          {routes.map((item) => <MenuItem key={item} value={item}>{item}</MenuItem>)}
        </TextField>
        <TextField select fullWidth margin="normal" label="Layout" value={layout} onChange={(event) => setLayout(event.target.value)}>
          <MenuItem value="">Recognise it from the first line</MenuItem>
          <MenuItem value="twilio">Twilio voice price file</MenuItem>
          <MenuItem value="faxbot">Faxbot layout</MenuItem>
        </TextField>
        <Button component="label" variant="outlined" size="small" sx={{ mt: 1 }}>
          {file ? file.name : 'Choose the CSV file'}
          <input hidden type="file" accept=".csv,text/csv" aria-label="Rate deck file"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        </Button>
        <Field label="Where it came from" value={source} onChange={setSource} placeholder="https://" />
        <Field label="Published or read on" type="date" value={published} onChange={setPublished} />
      </FormDialog>
    </>
  );
}

function ConfirmCaller({ client, caller, onDone }: {
  client: AdminAPIClient;
  caller: SendingCaller;
  onDone: (next: SendingCaller[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [evidence, setEvidence] = useState('');
  const [link, setLink] = useState('');
  const [bought, setBought] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await client.call<{ callers: SendingCaller[] }>({ method: 'POST', path: '/routing/caller-ids/confirm',
        body: { account: caller.account, caller_id: caller.caller_id, evidence: evidence.trim(),
          evidence_url: link.trim() || null, bought_here: bought } });
      setOpen(false);
      onDone(result.callers);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" onClick={() => { setError(null); setOpen(true); }}
        aria-label={`Confirm ${caller.caller_id} on ${caller.label}`}>Confirm</Button>
      <FormDialog open={open} title={`Confirm ${caller.caller_id} on ${caller.label}`} submitLabel="Confirm" busy={busy}
        canSubmit={Boolean(evidence.trim())} onSubmit={() => void submit()} onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Typography variant="body2" sx={{ mt: 1 }}>
          Confirm that your organisation holds this number and may send faxes from it on this account. Calls from it
          then get the price its carrier lists for this caller ID.
        </Typography>
        <Field label="How you know" value={evidence} onChange={setEvidence} multiline required
          helperText="For example, the number order or the invoice that lists it." />
        <Field label="Link to the evidence" value={link} onChange={setLink} placeholder="https://" />
        <FormControlLabel control={<Checkbox checked={bought} onChange={(event) => setBought(event.target.checked)} />}
          label="This number was bought on this account" />
        <Typography variant="caption" color="text.secondary" display="block">
          Telnyx gives its local price only to numbers bought on Telnyx.
        </Typography>
      </FormDialog>
    </>
  );
}

export default function CallerIdPrices({ client, routes, canWrite }: {
  client: AdminAPIClient;
  routes: string[];
  canWrite: boolean;
}) {
  const [prices, setPrices] = useState<Prices | null>(null);
  const [failed, setFailed] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [number, setNumber] = useState('');
  const [quotes, setQuotes] = useState<{ number: string; quotes: CallerIdQuote[] } | null>(null);
  const [quoteError, setQuoteError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      setPrices(await client.call<Prices>({ method: 'GET', path: '/routing/caller-id-prices' }));
      setFailed(false);
    } catch (failure) {
      setFailed(saysFailure(failure));
    }
  }, [client]);
  useEffect(() => { void load(); }, [load]);

  const lookUp = async () => {
    setQuoteError(null);
    try {
      setQuotes(await client.call({ method: 'GET',
        path: `/routing/caller-id-prices/quote?to=${encodeURIComponent(number.trim())}` }));
    } catch (failure) {
      setQuoteError(failure);
    }
  };

  const withdraw = async (caller: SendingCaller) => {
    setActionError(null);
    try {
      const result = await client.call<{ callers: SendingCaller[] }>({ method: 'POST', path: '/routing/caller-ids/withdraw',
        body: { account: caller.account, caller_id: caller.caller_id, note: '' } });
      setPrices((current) => current && { ...current, callers: result.callers });
    } catch (failure) {
      setActionError(failure);
    }
  };

  if (failed) {
    return <LoadFailed testId="caller-id-prices-unread" text="Prices by caller ID could not be loaded. Try again." />;
  }
  if (!prices) return null;
  const deckRoutes = Array.from(new Set([...routes, ...prices.callers.filter((item) => item.priced_by_caller_id)
    .map((item) => item.account)]));
  return (
    <Box mt={3} data-testid="caller-id-prices">
      <Typography variant="h6">Prices by caller ID</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Some carriers charge much less for a call to a country when the caller ID comes from that country or the EEA.
        Faxbot uses the lower price only for a caller ID you confirmed on that account, and never changes the caller ID
        an account shows.
      </Typography>
      {message && <Alert severity="success" sx={{ mb: 1 }} onClose={() => setMessage(null)}>{message}</Alert>}
      {prices.decks.length === 0 ? (
        <Typography variant="body2">No rate deck priced by caller ID yet.</Typography>
      ) : prices.decks.map((deck) => (
        <Typography key={deck.route} variant="body2">
          {deck.route}: {deck.rows.toLocaleString()} prices ({Object.entries(deck.by_type)
            .map(([kind, count]) => `${count} ${FOR_TYPE[kind] ?? kind}`).join(', ')})
          {deck.published_on ? `, published or read on ${formatLocalDate(deck.published_on)}` : ''}
          {deck.source_url ? <> · <Link href={deck.source_url} target="_blank" rel="noreferrer">source</Link></> : null}
        </Typography>
      ))}
      {canWrite && (
        <Box my={1}>
          <ImportDeck client={client} routes={deckRoutes} layouts={prices.layouts}
            onDone={(text) => { setMessage(text); void load(); }} />
        </Box>
      )}
      <DeliveryError error={actionError} />
      <TableContainer component={Paper} sx={{ borderRadius: 2, mt: 1 }}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Account</TableCell>
              <TableCell>Caller ID its calls show</TableCell>
              <TableCell>Confirmed</TableCell>
              <TableCell>Evidence</TableCell>
              <TableCell align="right">Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {prices.callers.map((caller) => (
              <TableRow key={caller.account}>
                <TableCell>{caller.label}</TableCell>
                <TableCell>{caller.caller_id ?? '-'}</TableCell>
                <TableCell>{status(caller)}</TableCell>
                <TableCell>
                  {caller.eligibility?.state === 'confirmed' ? caller.eligibility.evidence : caller.sentence}
                </TableCell>
                <TableCell align="right">
                  {canWrite && caller.priced_by_caller_id && caller.caller_id && (
                    caller.eligibility?.state === 'confirmed'
                      ? <Button size="small" color="error" onClick={() => void withdraw(caller)}
                          aria-label={`Withdraw ${caller.caller_id} on ${caller.label}`}>Withdraw</Button>
                      : <ConfirmCaller client={client} caller={caller}
                          onDone={(next) => setPrices((current) => current && { ...current, callers: next })} />
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} mt={2}>
        <TextField size="small" label="Fax number" placeholder="+4930123456" value={number}
          onChange={(event) => setNumber(event.target.value)} />
        <Button size="small" variant="outlined" disabled={!number.trim()} onClick={() => void lookUp()}>Show prices</Button>
      </Stack>
      <DeliveryError error={quoteError} />
      {quotes && (
        <Box mt={1} data-testid="caller-id-quotes">
          {quotes.quotes.length === 0 ? (
            <Typography variant="body2">No rate deck priced by caller ID covers {quotes.number}.</Typography>
          ) : quotes.quotes.map((item) => (
            <Typography key={item.account} variant="body2"><strong>{item.label}:</strong> {item.sentence}</Typography>
          ))}
        </Box>
      )}
    </Box>
  );
}
