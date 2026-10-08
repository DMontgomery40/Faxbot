// Costs → Spending → Partner relays: what partners relayed for you and what you relayed for them, with each
// side's own money (the relay's charge, and the sender's price from the relay's signed statement).
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { RelayCost, RelayedFax } from '../../api/deliveryTypes';
import { shortTime } from '../work/text';
import { DeliveryError } from './shared';

export default function RelayCosts({ client }: { client: AdminAPIClient }) {
  const [costs, setCosts] = useState<RelayCost[] | null>(null);
  const [faxes, setFaxes] = useState<RelayedFax[]>([]);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      const [money, sent] = await Promise.all([client.getRelayCosts(), client.listRelayedFaxes()]);
      setCosts(money.agreements);
      setFaxes(sent.faxes);
    } catch (failure) {
      setError(failure);
      setCosts([]);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  // Nothing went through a relay: the section stays out of the way.
  if (costs === null || (costs.length === 0 && faxes.length === 0 && !error)) return null;
  return (
    <Box component="section" mt={4} data-testid="relay-costs">
      <Typography variant="h6" component="h2">Partner relays</Typography>
      <Typography variant="body2" color="text.secondary" mb={2}>
        Faxes partners sent for you as local calls in their country, and faxes you sent for them, over the last 30 days.
      </Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Stack spacing={1} mb={2}>
        {costs.map((item) => (
          <Typography key={`${item.role}-${item.agreement_id}`} variant="body1" data-testid="relay-cost">{item.sentence}</Typography>
        ))}
      </Stack>
      {faxes.length > 0 && (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Sent</TableCell>
                <TableCell>For</TableCell>
                <TableCell>Partner</TableCell>
                <TableCell>Fax number</TableCell>
                <TableCell align="right">Pages</TableCell>
                <TableCell>Result</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {faxes.map((fax) => (
                <TableRow key={`${fax.role}-${fax.fax_id}`}>
                  <TableCell>{shortTime(fax.created_at)}</TableCell>
                  <TableCell>{fax.role === 'relay' ? 'Them' : 'You'}</TableCell>
                  <TableCell>{fax.partner}</TableCell>
                  <TableCell>{fax.fax_number}</TableCell>
                  <TableCell align="right">{fax.pages ?? '-'}</TableCell>
                  <TableCell>{fax.status}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Box>
  );
}
