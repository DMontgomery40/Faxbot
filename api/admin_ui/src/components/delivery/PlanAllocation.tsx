// Savings & optimization → Prices & plans → Who gets your plans' last pages: for each plan with a limited allowance or normal-use
// budget, what is left until it starts again, which waiting faxes get it (the ones it saves the most on), which go
// another way and for how much, and what Faxbot keeps for faxes not sent yet. Every amount is an estimate; nothing
// here changes a setting.
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Chip, CircularProgress, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { PlanAllocation as PlanAllocationData, PlanAllocationFax, PlanAllocationPlan } from '../../api/deliveryTypes';
import { formatServerTime } from '../../api/time';
import { DeliveryError } from './shared';

const OUTCOMES: Record<PlanAllocationFax['outcome'], { label: string; color: 'success' | 'default' | 'info' }> = {
  plan: { label: 'Gets the plan', color: 'success' },
  forced: { label: 'Takes the plan', color: 'info' },
  other: { label: 'Goes another way', color: 'default' },
};

function Plan({ plan }: { plan: PlanAllocationPlan }) {
  const unit = plan.unit === 'minutes' ? 'Minutes' : 'Pages counted';
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid={`plan-allocation-${plan.route}`}>
      <Typography variant="subtitle1" component="h3" fontWeight={600}>{plan.name}</Typography>
      <Typography variant="body1" mb={plan.faxes.length ? 1 : 0}>{plan.sentence}</Typography>
      {plan.faxes.length > 0 && (
        <TableContainer>
          <Table size="small" aria-label={`Waiting faxes for ${plan.name}`}>
            <TableHead>
              <TableRow>
                <TableCell>To</TableCell>
                <TableCell align="right">Pages</TableCell>
                <TableCell align="right">{unit}</TableCell>
                <TableCell>Waiting since</TableCell>
                <TableCell>Outcome</TableCell>
                <TableCell>Why</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {plan.faxes.map((fax) => (
                <TableRow key={fax.job_id}>
                  <TableCell>{fax.to}</TableCell>
                  <TableCell align="right">{fax.pages.toLocaleString()}</TableCell>
                  <TableCell align="right">{fax.units.toLocaleString()}</TableCell>
                  <TableCell>{formatServerTime(fax.queued_at)}</TableCell>
                  <TableCell>
                    <Chip size="small" label={OUTCOMES[fax.outcome].label} color={OUTCOMES[fax.outcome].color} />
                  </TableCell>
                  <TableCell>{fax.sentence}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      <Stack spacing={0.5} mt={1}>
        {[plan.saving_sentence, plan.reserve_sentence, plan.bound_sentence].filter(Boolean).map((sentence) => (
          <Typography key={sentence} variant="body2" color="text.secondary">{sentence}</Typography>
        ))}
      </Stack>
    </Paper>
  );
}

export default function PlanAllocation({ client }: { client: AdminAPIClient }) {
  const [data, setData] = useState<PlanAllocationData | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      setData(await client.getPlanAllocation());
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Box component="section" mt={4} data-testid="plan-allocation">
      <Typography variant="h6" component="h2">Who gets your plans&apos; last pages</Typography>
      <Typography variant="body2" color="text.secondary" mb={2}>
        When a plan has only a few included pages or minutes left, Faxbot gives them to the waiting faxes they save the
        most on and sends the others by their next cheapest route. Every amount is an estimate.
      </Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data && !error && <CircularProgress size={24} />}
      {data && data.plans.length === 0 && (
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
          <Typography variant="body1" data-testid="plan-allocation-empty">{data.empty_sentence}</Typography>
        </Paper>
      )}
      {data && data.plans.length > 0 && (
        <Stack spacing={2}>
          {data.plans.map((plan) => <Plan key={plan.route} plan={plan} />)}
        </Stack>
      )}
    </Box>
  );
}
