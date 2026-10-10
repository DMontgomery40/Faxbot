// Savings & optimization → Prices & plans → Exchange rates: the rates Faxbot compares prices in different currencies
// with. The ECB reference rates are read by default; your own rate (such as the one on your invoice) wins while it is
// current. Every price stays in its own currency; only the comparison converts.
import { useCallback, useEffect, useState } from 'react';
import { Box, Button, FormControlLabel, Link, Paper, Stack, Switch, Table, TableBody, TableCell, TableHead, TableRow,
  Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import { Field, FormDialog } from '../access/AccessViews';
import { DeliveryError } from './shared';

interface OwnRate { base: string; quote: string; rate: string; date: string; date_text: string; current: boolean;
  sentence: string; note: string | null }

export interface ExchangeView {
  reference: { enabled: boolean; sentence: string; date: string | null; date_text: string | null; current: boolean;
    source_url: string; read_on: string; rates: { currency: string; per_euro: string }[] };
  own: OwnRate[];
  reference_max_age_days: number;
  own_max_age_days: number;
  refreshed?: { outcome: string; error: string | null; date: string | null };
}

export const EXCHANGE_INTRO = 'Faxbot compares prices in different currencies only to choose a route. Each price stays '
  + 'in its own currency, and wherever a converted amount is shown, the original amount and the rate are shown too.';

function usable(value: unknown): value is ExchangeView {
  const view = value as ExchangeView | null;
  return Boolean(view && view.reference && Array.isArray(view.own));
}

function SetRate({ client, days, onDone }: { client: AdminAPIClient; days: number; onDone: (next: ExchangeView) => void }) {
  const [open, setOpen] = useState(false);
  const [base, setBase] = useState('');
  const [rate, setRate] = useState('');
  const [quote, setQuote] = useState('');
  const [day, setDay] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await client.call<ExchangeView>({ method: 'PUT', path: '/routing/exchange-rates/own', body: {
        base: base.trim().toUpperCase(), quote: quote.trim().toUpperCase(), rate: rate.trim(),
        rate_date: day || null, note: note.trim() || null } }));
      setOpen(false);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const preview = base.trim() && rate.trim() && quote.trim()
    ? `1 ${base.trim().toUpperCase()} = ${rate.trim()} ${quote.trim().toUpperCase()}` : null;
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>Set your own rate</Button>
      <FormDialog open={open} title="Set your own exchange rate" submitLabel="Save" busy={busy}
        canSubmit={Boolean(base.trim().length === 3 && quote.trim().length === 3 && rate.trim())}
        onClose={() => setOpen(false)} onSubmit={() => void save()}>
        <DeliveryError error={error} />
        <Typography variant="body2" mb={2}>
          Use the rate your bank or card charged, such as the one on your invoice. Faxbot uses it instead of the ECB
          rate for this pair of currencies for {days} days from its date.
        </Typography>
        <Field label="One unit of (currency code)" value={base} onChange={setBase} placeholder="GBP" />
        <Field label="Is worth" value={rate} onChange={setRate} placeholder="1.27" />
        <Field label="Of (currency code)" value={quote} onChange={setQuote} placeholder="USD" />
        <Field label="The rate's date" type="date" value={day} onChange={setDay}
          helperText="Such as the date on your invoice. Leave empty for today." />
        <Field label="Where it comes from" value={note} onChange={setNote} placeholder="September invoice" />
        {preview && <Typography variant="body2" data-testid="exchange-preview">{preview}</Typography>}
      </FormDialog>
    </>
  );
}

export default function ExchangeRates({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [view, setView] = useState<ExchangeView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const found = await client.call<unknown>({ method: 'GET', path: '/routing/exchange-rates' });
      setView(usable(found) ? found : null);
      if (!usable(found)) setError(new Error('Exchange rates could not be loaded. Try again.'));
    } catch (failure) {
      setError(failure);
    }
  }, [client]);
  useEffect(() => { void load(); }, [load]);
  const act = async (method: 'PUT' | 'POST', path: string, body?: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const next = await client.call<ExchangeView>({ method, path, body });
      setView(next);
      if (next.refreshed) {
        setMessage(next.refreshed.outcome === 'failed'
          ? `Faxbot could not read the ECB reference rates: ${next.refreshed.error}`
          : next.refreshed.outcome === 'unchanged'
            ? `The ECB has published nothing newer than the rates of ${next.reference.date_text}.`
            : `Read the ECB reference rates of ${next.reference.date_text}.`);
      }
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  if (!view) return error ? <DeliveryError error={error} /> : null;
  const { reference } = view;
  return (
    <Paper variant="outlined" sx={{ p: 3, borderRadius: 2, mt: 3 }} data-testid="exchange-rates">
      <Typography variant="h6" component="h2">Exchange rates</Typography>
      <Typography variant="body2" color="text.secondary" mb={2}>{EXCHANGE_INTRO}</Typography>
      <DeliveryError error={error} />
      <FormControlLabel
        control={<Switch checked={reference.enabled} disabled={!canWrite || busy}
          onChange={(event) => void act('PUT', '/routing/exchange-rates/reference', { enabled: event.target.checked })} />}
        label="Read the ECB reference rates" />
      <Typography variant="body2" data-testid="exchange-reference-sentence">{reference.sentence}</Typography>
      <Typography variant="body2" color="text.secondary">
        Published by the European Central Bank every working day at about 4 PM in Frankfurt, for information; Faxbot
        uses them only to compare prices, for up to {view.reference_max_age_days} days. <Link href={reference.source_url}
          target="_blank" rel="noreferrer">ECB reference rates</Link>
      </Typography>
      {message && <Typography variant="body2" mt={1} data-testid="exchange-refresh-message">{message}</Typography>}
      {canWrite && reference.enabled && (
        <Button size="small" sx={{ mt: 1 }} disabled={busy}
          onClick={() => void act('POST', '/routing/exchange-rates/refresh')}>Read them now</Button>
      )}
      <Typography variant="subtitle1" fontWeight={600} mt={3}>Your own rates</Typography>
      {view.own.length === 0
        ? <Typography variant="body2" color="text.secondary">You have not set any rates of your own.</Typography>
        : (
          <Table size="small" aria-label="Your own rates">
            <TableHead>
              <TableRow><TableCell>Rate</TableCell><TableCell>Its date</TableCell><TableCell>Used to compare</TableCell>
                <TableCell>From</TableCell><TableCell /></TableRow>
            </TableHead>
            <TableBody>
              {view.own.map((item) => (
                <TableRow key={`${item.base}-${item.quote}`}>
                  <TableCell>1 {item.base} = {item.rate} {item.quote}</TableCell>
                  <TableCell>{item.date_text}</TableCell>
                  <TableCell>{item.current ? 'Yes' : `No, more than ${view.own_max_age_days} days old`}</TableCell>
                  <TableCell>{item.note || ''}</TableCell>
                  <TableCell>
                    {canWrite && <Button size="small" disabled={busy} aria-label={`Stop using your ${item.base} to ${item.quote} rate`}
                      onClick={() => void act('POST', '/routing/exchange-rates/own/clear', { base: item.base, quote: item.quote })}>
                      Stop using</Button>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      {canWrite && <Stack direction="row" spacing={1} mt={2}><SetRate client={client} days={view.own_max_age_days} onDone={setView} /></Stack>}
      {reference.rates.length > 0 && (
        <Box mt={3}>
          <Typography variant="subtitle1" fontWeight={600}>ECB reference rates of {reference.date_text}, per euro</Typography>
          <Typography variant="body2" color="text.secondary" data-testid="exchange-reference-rates">
            {reference.rates.map((item) => `${item.currency} ${item.per_euro}`).join(' · ')}
          </Typography>
        </Box>
      )}
    </Paper>
  );
}
