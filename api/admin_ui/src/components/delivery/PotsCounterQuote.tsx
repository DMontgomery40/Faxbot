// Costs → Recommendations → POTS replacement (N23): take the fax lines out of a POTS-replacement order, from the
// line inventory and the quote you enter. Advice only: nothing orders, ports or cancels a line.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Link, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import { formatLocalDate } from '../../api/time';
import { Field, FormDialog } from '../access/AccessViews';
import { DeliveryError } from './shared';

interface Published { id: string; name: string; per_line: string; sentence: string; source: string; label: string; read_on: string }

interface CounterQuote {
  name: string;
  per_line: string;
  source_url: string | null;
  source_date: string | null;
  sentences: string[];
}

export interface PotsView {
  quotes: CounterQuote[];
  published: Published[];
  sentence: string | null;
  note: string;
}

function usable(value: unknown): value is PotsView {
  const view = value as PotsView | null;
  return Boolean(view && Array.isArray(view.quotes) && Array.isArray(view.published));
}

function EnterQuote({ client, published, onDone }: { client: AdminAPIClient; published: Published[]; onDone: (next: PotsView) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [perLine, setPerLine] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [term, setTerm] = useState('');
  const [lines, setLines] = useState('');
  const [ports, setPorts] = useState('');
  const [devicePrice, setDevicePrice] = useState('');
  const [source, setSource] = useState('');
  const [sourceDate, setSourceDate] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const whole = (text: string) => (text.trim() ? Number(text) : null);
  const save = async (body: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    try {
      onDone(await client.call<PotsView>({ method: 'PUT', path: '/routing/pots-quotes', body }));
      setOpen(false);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" onClick={() => { setError(null); setOpen(true); }}>Enter a quote</Button>
      <FormDialog open={open} title="Enter a POTS-replacement quote" submitLabel="Save" busy={busy}
        canSubmit={Boolean(name.trim() && perLine.trim())} onClose={() => setOpen(false)}
        onSubmit={() => void save({ name: name.trim(), per_line: perLine.trim(), currency: currency.trim(), term_months: whole(term),
          lines_quoted: whole(lines), ports_per_device: whole(ports), device_price: devicePrice.trim(),
          source_url: source.trim() || null, source_date: sourceDate || null })}>
        <DeliveryError error={error} />
        {published.map((item) => (
          <Box key={item.id} mb={1}>
            <Typography variant="body2">{item.sentence} <Link href={item.source} target="_blank" rel="noreferrer">{item.label}</Link>.</Typography>
            <Button size="small" disabled={busy} onClick={() => void save({ published: item.id, term_months: whole(term), lines_quoted: whole(lines) })}>
              Use {item.name}'s published price
            </Button>
          </Box>
        ))}
        <Field label="Product quoted" value={name} onChange={setName} placeholder="Ooma AirDial" />
        <Field label="Price per line a month" value={perLine} onChange={setPerLine} placeholder="39.95" />
        <Field label="Currency" value={currency} onChange={setCurrency} />
        <Field label="Term in months" value={term} onChange={setTerm} type="number" />
        <Field label="Lines the quote covers" value={lines} onChange={setLines} type="number"
          helperText="Leave empty to use every line in your inventory." />
        <Field label="Analog ports on one device" value={ports} onChange={setPorts} type="number" />
        <Field label="One-time price of one device" value={devicePrice} onChange={setDevicePrice} />
        <Field label="Where the price comes from" value={source} onChange={setSource} />
        <Field label="The quote's date" type="date" value={sourceDate} onChange={setSourceDate} />
      </FormDialog>
    </>
  );
}

export default function PotsCounterQuote({ client, canWrite, onCount }: {
  client: AdminAPIClient; canWrite: boolean; onCount?: (count: number | null) => void;
}) {
  const [view, setView] = useState<PotsView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(async () => {
    try {
      const found = await client.call<unknown>({ method: 'GET', path: '/routing/pots-quotes' });
      setView(usable(found) ? found : null);
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    }
  }, [client, onCount]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (view) onCount?.(view.quotes.length); }, [view, onCount]);
  const withdraw = async (name: string) => {
    setError(null);
    try {
      setView(await client.call<PotsView>({ method: 'POST', path: '/routing/pots-quotes/remove', body: { name } }));
    } catch (failure) {
      setError(failure);
    }
  };
  if (!view) return error ? <DeliveryError error={error} /> : null;
  return (
    <Paper variant="outlined" sx={{ p: 3, borderRadius: 2 }} data-testid="pots-counter-quote">
      <Typography variant="h6" component="h2">Fax lines in a POTS-replacement order</Typography>
      <DeliveryError error={error} />
      {view.sentence && <Typography variant="body2" mt={1}>{view.sentence}</Typography>}
      {view.quotes.map((quote) => (
        <Box key={quote.name} mt={2}>
          <Typography variant="subtitle1" fontWeight={600}>
            {quote.name} at {quote.per_line} a line a month
            {quote.source_url ? <> · <Link href={quote.source_url} target="_blank" rel="noreferrer">source</Link></> : null}
            {quote.source_date ? `, ${formatLocalDate(quote.source_date)}` : ''}
          </Typography>
          {quote.sentences.map((sentence, index) => (
            index === 1 ? <Alert key={index} severity="info" sx={{ my: 0.5 }}>{sentence}</Alert>
              : <Typography key={index} variant="body2">{sentence}</Typography>
          ))}
          {canWrite && <Button size="small" onClick={() => void withdraw(quote.name)}
            aria-label={`Withdraw the ${quote.name} quote`}>Withdraw</Button>}
        </Box>
      ))}
      {canWrite && (
        <Stack direction="row" spacing={1} mt={2}>
          <EnterQuote client={client} published={view.published} onDone={setView} />
        </Stack>
      )}
      <Typography variant="body2" color="text.secondary" mt={1}>{view.note}</Typography>
    </Paper>
  );
}
