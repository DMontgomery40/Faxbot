// Savings & optimization → Opportunities → Where each number should live: what each of your numbers costs at the account that
// carries it and at your other accounts, and the steps to move (port) one where it costs less. Advice only: Faxbot
// never moves a number. Your NPI record has its own page under Numbers (NpiRecord.tsx).
import { useEffect, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Chip, Link, Paper, Stack, Table, TableBody,
  TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import AdminAPIClient from '../../api/client';
import type { NumberPlacement as Placement, PortingSteps } from '../../api/numberAdviceTypes';
import { readOnText } from './ReceivingRecommendations';
import { formatMoneyList } from './shared';

export function PortingStepsView({ steps }: { steps: PortingSteps }) {
  return (
    <Box data-testid="porting-steps">
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{`To move it from ${steps.from} to ${steps.to}:`}</Typography>
      <Box component="ol" sx={{ mt: 0.5, mb: 0.5, pl: 3 }}>
        {steps.steps.map((step) => <li key={step}><Typography variant="body2">{step}</Typography></li>)}
      </Box>
      <Typography variant="body2">{`Fee: ${steps.fee}`}</Typography>
      <Typography variant="body2">{`Usual time: ${steps.lead_time}`}</Typography>
      {steps.restriction && <Typography variant="body2" color="warning.main">{steps.restriction}</Typography>}
      <Stack direction="row" spacing={1.5} sx={{ mt: 0.5, flexWrap: 'wrap' }}>
        {steps.sources.map((source) => (
          <Typography key={source.url} variant="caption" color="text.secondary">
            <Link href={source.url} target="_blank" rel="noreferrer">{source.label ?? source.url}</Link>
            {` (read ${readOnText(source.read_on)}${source.secondary ? ', review site' : ''})`}
          </Typography>
        ))}
      </Stack>
    </Box>
  );
}

export default function NumberPlacement({ client, onCount }: {
  client: AdminAPIClient;
  onCount?: (count: number | null) => void;
}) {
  const [placement, setPlacement] = useState<Placement | null>(null);
  useEffect(() => {
    let live = true;
    client.call<Placement>({ method: 'GET', path: '/routing/recommendations/numbers' })
      .then((found) => {
        if (!live) return;
        setPlacement(found);
        onCount?.(found.numbers.filter((row) => row.state === 'move').length + found.accounts.length);
      })
      .catch(() => { if (live) onCount?.(null); });
    return () => { live = false; };
  }, [client, onCount]);
  if (!placement || placement.state === 'no_numbers') return null;
  const advice = placement.numbers.filter((row) => row.state !== 'keep' || row.notes.length > 0);
  return (
    <Stack spacing={2}>
      {(
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="number-placement">
          <Box display="flex" alignItems="center" gap={1} sx={{ mb: 1 }}>
            <Typography variant="h6" component="h3">Where each number should live</Typography>
            <Chip size="small" variant="outlined" label="Estimate" />
          </Box>
          <Typography variant="body1" data-testid="number-placement-sentence">{placement.sentence}</Typography>
          <TableContainer sx={{ mt: 2 }}>
            <Table size="small" aria-label="Where each number should live">
              <TableHead>
                <TableRow>
                  <TableCell>Number</TableCell>
                  <TableCell>Lives at</TableCell>
                  <TableCell align="right">{`Faxes received, last ${placement.days} days`}</TableCell>
                  <TableCell align="right">A month there</TableCell>
                  <TableCell>Cheapest</TableCell>
                  <TableCell align="right">Saves a month</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {placement.numbers.map((row) => (
                  <TableRow key={row.number}>
                    <TableCell>{row.display}</TableCell>
                    <TableCell>{row.account}</TableCell>
                    <TableCell align="right">{row.received}</TableCell>
                    <TableCell align="right">
                      {formatMoneyList(row.costs.find((cost) => cost.current)?.monthly, 'Not known')}
                    </TableCell>
                    <TableCell>{row.cheapest ?? row.account}</TableCell>
                    <TableCell align="right">{formatMoneyList(row.saving, '-')}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          <Stack spacing={1.5} sx={{ mt: 2 }}>
            {advice.map((row) => (
              <Box key={`${row.number}-advice`}>
                <Typography variant="body2">{row.sentence}</Typography>
                {row.notes.map((note) => <Typography key={note} variant="body2" color="text.secondary">{note}</Typography>)}
                {row.porting && <PortingStepsView steps={row.porting} />}
              </Box>
            ))}
            {placement.accounts.map((account) => (
              <Box key={account.account} data-testid="account-worth">
                <Alert severity="info">{account.sentence}</Alert>
                {account.porting.map((steps) => <PortingStepsView key={`${steps.from}-${steps.to}`} steps={steps} />)}
              </Box>
            ))}
          </Stack>
          {placement.assumptions && placement.assumptions.length > 0 && (
            <Accordion disableGutters elevation={0} sx={{ mt: 1, '&:before': { display: 'none' } }}>
              <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                <Typography variant="body2">How Faxbot worked this out</Typography>
              </AccordionSummary>
              <AccordionDetails>
                {placement.assumptions.map((line) => <Typography key={line} variant="body2">{line}</Typography>)}
              </AccordionDetails>
            </Accordion>
          )}
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{placement.note}</Typography>
        </Paper>
      )}
    </Stack>
  );
}
