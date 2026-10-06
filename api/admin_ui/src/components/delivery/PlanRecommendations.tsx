// Costs → Recommendations → Plans: whether each monthly plan is worth its fee at your traffic. Every figure is an
// estimate over the days Faxbot has records for; Faxbot advises only and never cancels or changes a plan.
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Chip, CircularProgress, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { PlanAdvice, PlanRecommendations as Result, PlanWindow } from '../../api/deliveryTypes';
import { DeliveryError, formatMoneyList } from './shared';

const STATE_LABELS: Record<PlanAdvice['state'], string> = {
  keep: 'Keep it', review: 'Worth reviewing', too_little_history: 'Not enough history yet',
};

const ROWS: Array<[string, (window: PlanWindow) => string]> = [
  ['Days Faxbot has records for', (window) => String(window.days)],
  ['Faxes sent', (window) => String(window.sent)],
  ['Faxes received', (window) => String(window.received)],
  ['Of these, test faxes to your own numbers', (window) => String(window.own_numbers)],
  ['Plan fee for this period', (window) => formatMoneyList(window.fee, '-')],
  ['Plan fee per fax', (window) => formatMoneyList(window.fee_per_fax, '-')],
  ['The same faxes another way', (window) => formatMoneyList(window.other_way, '-')],
  ['Rent for the fax number at your carrier', (window) => formatMoneyList(window.number_rental, '-')],
];

function Plan({ plan, days }: { plan: PlanAdvice; days: number }) {
  const [latest, before] = plan.windows;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="plan-recommendation">
      <Box display="flex" alignItems="center" gap={1} flexWrap="wrap" sx={{ mb: 1 }}>
        <Typography variant="h6" component="h3">
          {`${plan.name}, ${formatMoneyList(plan.monthly_fee, '-')} a month`}
        </Typography>
        <Chip size="small" label={STATE_LABELS[plan.state]} color={plan.state === 'review' ? 'warning' : 'default'} />
        <Chip size="small" variant="outlined" label="Estimate" />
      </Box>
      <Typography variant="body1" data-testid="plan-sentence">{plan.sentence}</Typography>
      {plan.action && <Typography variant="body2" sx={{ mt: 1 }} data-testid="plan-action">{plan.action}</Typography>}
      {plan.caveats.length > 0 && (
        <Box component="ul" sx={{ mt: 1, mb: 0, pl: 3 }}>
          {plan.caveats.map((caveat) => (
            <Typography key={caveat} component="li" variant="body2" color="text.secondary">{caveat}</Typography>
          ))}
        </Box>
      )}
      {plan.state !== 'too_little_history' && latest && before && (
        <TableContainer sx={{ mt: 2 }}>
          <Table size="small" aria-label={`${plan.name} plan compared with paying per fax`}>
            <TableHead>
              <TableRow>
                <TableCell>Estimate</TableCell>
                <TableCell align="right">{`Last ${days} days`}</TableCell>
                <TableCell align="right">{`The ${days} days before`}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {ROWS.map(([label, value]) => (
                <TableRow key={label}>
                  <TableCell>{label}</TableCell>
                  <TableCell align="right">{value(latest)}</TableCell>
                  <TableCell align="right">{before.days > 0 ? value(before) : '-'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  );
}

export default function PlanRecommendations({ client, onCount }: {
  client: AdminAPIClient;
  // Recommendations' empty sentence: how many plans Faxbot can judge yet; null on failure.
  onCount?: (count: number | null) => void;
}) {
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const loaded = await client.getPlanRecommendations();
      setData(loaded);
      onCount?.(loaded.plans.filter((plan) => plan.state !== 'too_little_history').length);
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    } finally {
      setBusy(false);
    }
  }, [client, onCount]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Box data-testid="plan-recommendations">
      <Typography variant="h5" component="h2" sx={{ mb: 1 }}>Plans</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data && busy && <CircularProgress size={24} />}
      {data && data.plans.length === 0 && (
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
          <Typography variant="body1" data-testid="plans-empty">{data.empty_sentence}</Typography>
        </Paper>
      )}
      {data && data.plans.length > 0 && (
        <Stack spacing={2}>
          {data.plans.map((plan) => <Plan key={plan.route} plan={plan} days={data.days} />)}
        </Stack>
      )}
    </Box>
  );
}
