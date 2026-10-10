// Savings & optimization → Opportunities → Calls by state: whether each trunk's carrier prices US calls by whether they stay within
// one state, which site's trunk would send a state's faxes for less, and the carrier price files Faxbot uses. Faxbot
// prices each call from where it really starts and never changes caller ID to lower a charge.
import { useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, Link, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField,
  Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { SiteAdvice as Advice } from '../../api/numberAdviceTypes';
import { readOnText } from './ReceivingRecommendations';
import { DeliveryError } from './shared';

function ImportPrices({ client, onImported }: { client: AdminAPIClient; onImported: () => void }) {
  const [carrier, setCarrier] = useState('anveo');
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState('');
  const [readOn, setReadOn] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<string | null>(null);
  const save = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await client.importStatePrices(carrier, file, { sourceUrl: source || undefined,
        readOn: readOn || undefined });
      const item = result.prices.find((price) => price.route === `sip-${carrier.replace(/^sip-/, '')}`);
      setDone(item ? `${item.carrier}: ${item.rows.toLocaleString()} number prefixes, ${item.differ.toLocaleString()} `
        + 'priced differently within one state.' : 'Prices saved.');
      onImported();
    } catch (problem) {
      setError(problem);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Box sx={{ mt: 2 }} data-testid="state-prices-import">
      <Typography variant="body2" sx={{ fontWeight: 600 }}>Add a carrier's prices by state</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        A price file (CSV) with a price for calls between states and within one state, such as AnveoDirect's rate file.
      </Typography>
      {error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null}
      {done && <Alert severity="success" sx={{ mb: 1 }}>{done}</Alert>}
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}>
        <TextField size="small" label="Carrier" value={carrier} onChange={(event) => setCarrier(event.target.value.trim())} />
        <Button variant="outlined" component="label">
          {file ? file.name : 'Choose file'}
          <input hidden type="file" accept=".csv,text/csv" data-testid="state-prices-file"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
        </Button>
        <TextField size="small" label="Where you got it (optional)" value={source}
          onChange={(event) => setSource(event.target.value)} />
        <TextField size="small" type="date" label="Downloaded on" value={readOn} InputLabelProps={{ shrink: true }}
          onChange={(event) => setReadOn(event.target.value)} />
        <Button variant="contained" disabled={busy || !file || !carrier} onClick={() => void save()}>Add prices</Button>
      </Stack>
    </Box>
  );
}

export default function SiteAdvice({ client, canWrite = false, onCount }: {
  client: AdminAPIClient;
  canWrite?: boolean;
  onCount?: (count: number | null) => void;
}) {
  const [advice, setAdvice] = useState<Advice | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let live = true;
    client.call<Advice>({ method: 'GET', path: '/routing/recommendations/sites' })
      .then((found) => { if (live) { setAdvice(found); onCount?.(found.items.length); } })
      .catch(() => { if (live) onCount?.(null); });
    return () => { live = false; };
  }, [client, onCount, version]);
  if (!advice || advice.carriers.length === 0) return null;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="site-advice">
      <Box display="flex" alignItems="center" gap={1} sx={{ mb: 1 }}>
        <Typography variant="h6" component="h3">Calls by state</Typography>
        <Chip size="small" variant="outlined" label="Estimate" />
      </Box>
      <Typography variant="body1" data-testid="site-advice-sentence">{advice.sentence}</Typography>
      <Stack spacing={1} sx={{ mt: 1 }}>
        {advice.carriers.filter((carrier) => carrier.sentence !== advice.sentence).map((carrier) => (
          <Typography key={carrier.account} variant="body2">{carrier.sentence}</Typography>
        ))}
        {advice.items.map((item) => (
          <Alert key={`${item.from_site}-${item.to_site}-${item.state}`} severity="info">
            {item.sentence} {item.action}
          </Alert>
        ))}
      </Stack>
      {advice.prices.length > 0 && (
        <Table size="small" aria-label="US prices by where a call starts" sx={{ mt: 2 }}>
          <TableHead>
            <TableRow>
              <TableCell>Carrier</TableCell>
              <TableCell align="right">Number prefixes</TableCell>
              <TableCell align="right">Priced differently within a state</TableCell>
              <TableCell>Read on</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {advice.prices.map((price) => (
              <TableRow key={price.route}>
                <TableCell>
                  {price.source_url ? <Link href={price.source_url} target="_blank" rel="noreferrer">{price.carrier}</Link>
                    : price.carrier}
                </TableCell>
                <TableCell align="right">{price.rows.toLocaleString()}</TableCell>
                <TableCell align="right">{price.differ.toLocaleString()}</TableCell>
                <TableCell>{readOnText(price.read_on)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {canWrite && <ImportPrices client={client} onImported={() => setVersion((value) => value + 1)} />}
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>{advice.caller_id}</Typography>
    </Paper>
  );
}
