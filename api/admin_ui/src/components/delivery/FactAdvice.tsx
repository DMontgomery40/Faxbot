// Savings & optimization → Facts to establish: what one missing fact cost you. For the last 90 days, per recipient, what the faxes cost by the best
// route Faxbot may use, and what they would have cost had one fact been established (a partner, a recipient's
// approval, a price, a plan's allowance), less what establishing it costs. Never savings: Savings & optimization → Savings results counts what
// an established fact really saved. Advice only (routing/fact_advice.py).
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../../api/client';
import type { FactAdvice as Advice, FactRecipient, FactRow } from '../../api/factAdviceTypes';
import { ScreenHeader } from '../access/AccessViews';
import { formatMoneyList } from './shared';
import PortfolioPlanner from './PortfolioPlanner';

const KIND_LABEL: Record<FactRow['kind'], string> = {
  authorization: 'Needs their agreement',
  information: 'Faxbot can learn it',
  price: 'Needs a price',
};

function FactCard({ row }: { row: FactRow }) {
  return (
    <Box sx={{ borderLeft: 3, borderColor: row.boundary ? 'warning.main' : 'divider', pl: 1.5 }} data-testid={`fact-${row.fact}`}>
      <Box display="flex" alignItems="center" gap={1} flexWrap="wrap">
        <Typography variant="subtitle2" component="h4">{row.title}</Typography>
        <Chip size="small" variant="outlined" label={KIND_LABEL[row.kind]} />
      </Box>
      <Typography variant="body2">{row.sentence}</Typography>
      <Typography variant="body2" color="text.secondary">{`Effort: ${row.establish}.`}</Typography>
      {(row.unknown || row.net.length === 0) && <Typography variant="body2">Expected benefit: unknown.</Typography>}
      <Typography variant="body2">Cautious benefit: unknown.</Typography>
      <Typography variant="body2" color="text.secondary">{row.confirm}</Typography>
      <Typography variant="body2" color="text.secondary">{row.step}</Typography>
    </Box>
  );
}

function RecipientCard({ item }: { item: FactRecipient }) {
  const title = item.name ? `${item.name} (${item.display})` : item.display;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="fact-recipient">
      <Box display="flex" justifyContent="space-between" alignItems="baseline" gap={1} flexWrap="wrap">
        <Typography variant="h6" component="h3">{title}</Typography>
        {item.largest.length > 0 && (
          <Typography variant="body2">{`Up to ${formatMoneyList(item.largest)} less (estimate)`}</Typography>
        )}
      </Box>
      {item.sentence && <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{item.sentence}</Typography>}
      <Stack spacing={1.5}>
        {item.facts.map((row) => <FactCard key={row.fact} row={row} />)}
      </Stack>
    </Paper>
  );
}

export default function FactAdvice({ client, embedded = false, onCount }: {
  client: AdminAPIClient; embedded?: boolean; onCount?: (count: number | null) => void;
}) {
  const [advice, setAdvice] = useState<Advice | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    setAdvice(null);
    onCount?.(null);
    setError(null);
    try {
      const result = await client.call<Advice>({ method: 'GET', path: '/routing/recommendations/facts' });
      setAdvice(result);
      onCount?.(result.recipients.reduce((count, item) => count + item.facts.length, 0));
    } catch (reason) {
      setError(reason instanceof AdminAPIError && reason.status === 403
        ? 'You do not have permission to view missing-fact advice.'
        : "Faxbot couldn't work out this advice just now. Try again in a minute.");
    } finally {
      setBusy(false);
    }
  }, [client, onCount]);
  useEffect(() => { void load(); }, [load]);
  return (
    <Box>
      {embedded ? <Box display="flex" justifyContent="space-between" gap={2} mb={2}>
        <Typography variant="h5" component="h2">Facts to establish</Typography>
        <Button onClick={load} disabled={busy}>Refresh</Button>
      </Box> : <ScreenHeader title="Facts to establish" onRefresh={load} busy={busy}
        subtitle="Cheaper ways to reach a recipient that one confirmed fact would open" />}
      {busy && <Typography role="status">Loading missing-fact advice…</Typography>}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {advice && (
        <Stack spacing={2}>
          <Typography variant="body2">{`Evidence period: last ${advice.days} days.`}</Typography>
          <Typography variant="body1" data-testid="fact-advice-sentence">{advice.sentence}</Typography>
          {advice.recipients.map((item) => <RecipientCard key={`${item.kind}-${item.number}`} item={item} />)}
          <Alert severity="info">{advice.realized}</Alert>
          {advice.assumptions.map((line) => (
            <Typography key={line} variant="body2" color="text.secondary">{line}</Typography>
          ))}
          <Typography variant="body2" color="text.secondary">{advice.note}</Typography>
        </Stack>
      )}
      {!embedded && <PortfolioPlanner client={client} />}
    </Box>
  );
}
