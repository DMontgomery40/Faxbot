// Savings & optimization → Opportunities → Your trunks: each trunk's monthly fee, busiest time, faxes and cost per fax, and when
// one trunk's faxes fit on another with what that would save. Advice only: Faxbot never cancels a trunk.
import { useEffect, useState } from 'react';
import { Alert, Paper, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';

export interface TrunkAdviceRow {
  key: string;
  label: string;
  lines: number;
  peak_lines: number;
  sent: number;
  received: number;
  monthly: string | null;
  cost_per_delivered: string | null;
}

export interface TrunkAdviceItem {
  trunk: string;
  into: string;
  sentence: string;
}

export interface TrunkAdviceResult {
  window_days: number;
  trunks: TrunkAdviceRow[];
  items: TrunkAdviceItem[];
  sentence: string | null;
}

export default function TrunkAdvice({ client, onCount }: {
  client: AdminAPIClient;
  onCount?: (count: number | null) => void;
}) {
  const [result, setResult] = useState<TrunkAdviceResult | null>(null);
  useEffect(() => {
    let live = true;
    client.call<TrunkAdviceResult>({ method: 'GET', path: '/routing/recommendations/trunks' })
      .then((found) => { if (live) { setResult(found); onCount?.(found.items.length); } })
      .catch(() => { if (live) onCount?.(null); });
    return () => { live = false; };
  }, [client, onCount]);
  // With one trunk there is nothing to compare; the section stays out of the way.
  if (!result || result.trunks.length < 2) return null;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="trunk-advice">
      <Typography variant="h6">Your trunks</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        The last {result.window_days} days on each trunk. Advice only: Faxbot never cancels a trunk.
      </Typography>
      <Table size="small" aria-label="Your trunks">
        <TableHead>
          <TableRow>
            <TableCell>Trunk</TableCell><TableCell>A month</TableCell><TableCell>Lines</TableCell>
            <TableCell>Most at once</TableCell><TableCell>Faxes sent</TableCell><TableCell>Faxes received</TableCell>
            <TableCell>Each sent fax</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {result.trunks.map((row) => (
            <TableRow key={row.key}>
              <TableCell>{row.label}</TableCell>
              <TableCell>{row.monthly ?? 'Not known'}</TableCell>
              <TableCell>{row.lines}</TableCell>
              <TableCell>{row.peak_lines}</TableCell>
              <TableCell>{row.sent}</TableCell>
              <TableCell>{row.received}</TableCell>
              <TableCell>{row.cost_per_delivered ?? '-'}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Stack spacing={1} sx={{ mt: 1.5 }}>
        {result.items.map((item) => <Alert key={`${item.trunk}-${item.into}`} severity="info">{item.sentence}</Alert>)}
        {result.sentence && <Typography variant="body2" color="text.secondary">{result.sentence}</Typography>}
      </Stack>
    </Paper>
  );
}
