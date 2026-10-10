// Savings & optimization → Opportunities → Shaded areas: whether keeping shaded areas with a fax-friendly pattern on documents
// you send would have saved time on your recent faxes (pages/friendly.py). It counts as a recommendation only when
// it would have; otherwise the section shows what Faxbot found, or nothing at all.
import { useCallback, useEffect, useState } from 'react';
import { Box, Chip, CircularProgress, Paper, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { FaxFriendlyRecommendation as Result } from '../../api/deliveryTypes';
import { DeliveryError } from './shared';
import friendlyWords from './faxFriendlySetting.json';

export const FRIENDLY_HEADING = friendlyWords.heading;

export default function FaxFriendlyRecommendation({ client, onCount }: {
  client: AdminAPIClient;
  // Recommendations' empty sentence: 1 when Faxbot recommends turning it on, 0 when not; null on failure.
  onCount?: (count: number | null) => void;
}) {
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const loaded = await client.getFaxFriendlyRecommendation();
      setData(loaded);
      onCount?.(loaded.recommend ? 1 : 0);
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    } finally {
      setBusy(false);
    }
  }, [client, onCount]);

  useEffect(() => { void load(); }, [load]);

  if (!error && !busy && (!data || !data.sentence)) return null;
  return (
    <Box data-testid="fax-friendly-recommendation">
      <Typography variant="h5" component="h2" sx={{ mb: 1 }}>{FRIENDLY_HEADING}</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data && busy && <CircularProgress size={24} />}
      {data?.sentence && (
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
          {data.recommend && (
            <Box display="flex" gap={1} sx={{ mb: 1 }}>
              <Chip size="small" color="warning" label="Worth turning on" />
              <Chip size="small" variant="outlined" label="Estimate" />
            </Box>
          )}
          <Typography variant="body1" data-testid="fax-friendly-sentence">{data.sentence}</Typography>
          {data.action && (
            <Typography variant="body2" sx={{ mt: 1 }} data-testid="fax-friendly-action">{data.action}</Typography>
          )}
        </Paper>
      )}
    </Box>
  );
}
