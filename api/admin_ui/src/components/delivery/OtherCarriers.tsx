// Costs → Recommendations → Other carriers: what your last 30 days of faxing would have cost at each carrier's
// published prices, the cheapest and the difference. Advice only: switching carriers means moving your numbers and
// a new account, and Faxbot never switches anything. A carrier missing a price for some faxes is never the cheapest.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Chip, CircularProgress, Link, Paper, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { CarrierComparison, CarrierPrice, Money } from '../../api/deliveryTypes';
import { formatLocalDate } from '../../api/time';
import { DeliveryError, formatMoney, formatMoneyList } from './shared';

// What a carrier's figure leaves out because it publishes no price for it.
function leftOut(row: CarrierPrice): string {
  const parts = [];
  if (row.not_priced) parts.push(`${row.not_priced} ${row.not_priced === 1 ? 'fax' : 'faxes'}`);
  if (row.numbers_not_priced) parts.push(`${row.numbers_not_priced} ${row.numbers_not_priced === 1 ? 'number' : 'numbers'}`);
  return parts.join(', ') || '-';
}

// Your current services' cost minus this carrier's: "$0.86 less", "$9.00 more", "Same", or "-" when unknown.
export function differenceText(difference: Money[]): string {
  const [value] = difference;
  if (!value) return '-';
  const negative = value.amount.startsWith('-');
  const size: Money = { currency: value.currency, amount: negative ? value.amount.slice(1) : value.amount };
  if (Number(size.amount) === 0) return 'Same';
  return `${formatMoney(size)} ${negative ? 'more' : 'less'}`;
}

function saves(row: CarrierPrice): boolean {
  const [value] = row.difference;
  return Boolean(value) && !row.yours && !value.amount.startsWith('-') && Number(value.amount) > 0;
}

export default function OtherCarriers({ client, onCount }: {
  client: AdminAPIClient;
  // Recommendations' empty sentence: how many carriers would have cost less; null on failure.
  onCount?: (count: number | null) => void;
}) {
  const [data, setData] = useState<CarrierComparison | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const loaded = await client.getCarrierRecommendations();
      setData(loaded);
      onCount?.(loaded.carriers.filter(saves).length);
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    } finally {
      setBusy(false);
    }
  }, [client, onCount]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Box data-testid="other-carriers">
      <Typography variant="h5" component="h2" sx={{ mb: 1 }}>Other carriers</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data && busy && <CircularProgress size={24} />}
      {data && (
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
          <Box display="flex" alignItems="center" gap={1} flexWrap="wrap" sx={{ mb: 1 }}>
            <Chip size="small" variant="outlined" label="Estimate" />
            <Chip size="small" variant="outlined" label="Advice only" />
          </Box>
          <Typography variant="body1" data-testid="carriers-sentence">{data.sentence}</Typography>
          {data.carriers.length > 0 && (
            <TableContainer sx={{ mt: 2 }}>
              <Table size="small" aria-label={`Your last ${data.days} days at each carrier's published prices`}>
                <TableHead>
                  <TableRow>
                    <TableCell>Carrier</TableCell>
                    <TableCell align="right">Estimate</TableCell>
                    <TableCell>Left out (no published price)</TableCell>
                    <TableCell align="right">Against now</TableCell>
                    <TableCell>Prices read on</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {data.carriers.map((row) => (
                    <TableRow key={row.id} data-testid="carrier-row">
                      <TableCell>
                        <Box display="flex" alignItems="center" gap={0.5} flexWrap="wrap">
                          <span>{row.name}</span>
                          {row.yours && <Chip size="small" label="Yours" />}
                          {row.cheapest && <Chip size="small" color="success" label="Cheapest" />}
                        </Box>
                        <Typography variant="caption" color="text.secondary" display="block">{row.sentence}</Typography>
                      </TableCell>
                      <TableCell align="right">{formatMoneyList(row.total)}</TableCell>
                      <TableCell>{leftOut(row)}</TableCell>
                      <TableCell align="right">{differenceText(row.difference)}</TableCell>
                      <TableCell>
                        {formatLocalDate(row.advertised_on)}
                        {row.source_url && <> · <Link href={row.source_url} target="_blank" rel="noreferrer">source</Link></>}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
          {data.unpublished_sentence && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }} data-testid="carriers-unpublished">
              {data.unpublished_sentence}
            </Typography>
          )}
          <Alert severity="info" sx={{ mt: 1.5 }} data-testid="carriers-switching">{data.switching_sentence}</Alert>
        </Paper>
      )}
    </Box>
  );
}
