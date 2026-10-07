// Costs → Savings: what sending together, direct delivery and case packets saved.
// Every figure is an estimate: it compares what Faxbot sent with calls and pages
// that never happened, so it stays an estimate after the carrier reports.
import { useCallback, useEffect, useState } from 'react';
import { Box, Chip, CircularProgress, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { Savings as SavingsResult } from '../../api/deliveryTypes';
import { parseServerTime } from '../../api/time';
import { ScreenHeader } from '../access/AccessViews';
import { DeliveryError, formatMoneyList } from './shared';

// The day case packets started being counted, in the reader's own words.
export function countedFromSentence(packets: SavingsResult['case_packets']): string | null {
  if (!packets.earlier_not_counted) return null;
  if (!packets.counted_from) return packets.counted_from_sentence;
  const day = parseServerTime(packets.counted_from);
  if (!day) return packets.counted_from_sentence;
  const date = day.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
  return `Counted from ${date}, when Faxbot started recording what each packet left out.`;
}

function Part({ title, sentence, note, testId }: { title: string; sentence: string; note?: string | null; testId: string }) {
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid={testId}>
      <Box display="flex" alignItems="center" gap={1} sx={{ mb: 1 }}>
        <Typography variant="h6" component="h2">{title}</Typography>
        <Chip size="small" variant="outlined" label="Estimate" />
      </Box>
      <Typography variant="body2">{sentence}</Typography>
      {note && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{note}</Typography>}
    </Paper>
  );
}

export default function Savings({ client }: { client: AdminAPIClient }) {
  const [data, setData] = useState<SavingsResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setData(await client.getSavings());
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Box>
      <ScreenHeader title="Savings" subtitle={data?.sentence} onRefresh={() => void load()} busy={busy} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data && busy && <CircularProgress size={24} />}
      {data && (
        <Stack spacing={2}>
          <Typography variant="body1" data-testid="savings-total">
            {/* The server's headline says honestly when something cost more than it saved. */}
            {data.total_sentence ?? (data.total_saved.length
              ? `About ${formatMoneyList(data.total_saved)} saved in the last ${data.days} days.`
              : `No money saved in the last ${data.days} days, as far as Faxbot can tell.`)}
          </Typography>
          <Part title="Sending together" sentence={data.sending_together.sentence} testId="savings-together" />
          {data.separator_pages && (
            <Part title="Separator pages left out" sentence={data.separator_pages.sentence} testId="savings-separator-pages" />
          )}
          <Part title="Direct delivery" sentence={data.direct_delivery.sentence} testId="savings-direct" />
          <Part title="Case packets" sentence={data.case_packets.sentence} note={countedFromSentence(data.case_packets)}
            testId="savings-packets" />
          {data.sslfax && <Part title="Faster pages" sentence={data.sslfax.sentence} testId="savings-sslfax" />}
          {data.own_numbers && <Part title="Faxes to your own numbers" sentence={data.own_numbers.sentence} testId="savings-own" />}
        </Stack>
      )}
    </Box>
  );
}
