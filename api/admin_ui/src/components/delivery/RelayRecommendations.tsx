// Savings & optimization → Opportunities → Partner relays: partners whose signed local price would have cost less than your own
// calls lately. Relaying stays off until both sides agree; this only says where it would have saved.
import { useCallback, useEffect, useState } from 'react';
import { Box, Chip, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { RelayRecommendation } from '../../api/deliveryTypes';
import { DeliveryError } from './shared';

export default function RelayRecommendations({ client, onCount }: {
  client: AdminAPIClient; onCount?: (count: number | null) => void;
}) {
  const [items, setItems] = useState<RelayRecommendation[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      const found = (await client.getRelayRecommendations()).recommendations;
      setItems(found);
      onCount?.(found.length);
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    }
  }, [client, onCount]);

  useEffect(() => { void load(); }, [load]);

  if (!error && (items === null || items.length === 0)) return null;
  return (
    <Box data-testid="advice-relays">
      <Box display="flex" alignItems="center" gap={1} sx={{ mb: 1 }}>
        <Typography variant="h5" component="h2">Partner relays</Typography>
        <Chip size="small" variant="outlined" label="Estimate" />
      </Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {items && items.length > 0 && (
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
          <Stack spacing={1.5}>
            {items.map((item) => (
              <Box key={`${item.peer_id}-${item.country}`} data-testid="relay-recommendation">
                <Typography variant="body1">{item.sentence}</Typography>
                <Typography variant="body2" color="text.secondary">{item.action}</Typography>
              </Box>
            ))}
          </Stack>
        </Paper>
      )}
    </Box>
  );
}
