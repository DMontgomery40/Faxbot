// Costs → Recommendations: ways to pay less, once Faxbot has some to offer.
// Each section loads its own recommendations and reports how many it shows; the
// empty sentence appears only when every section has none.
import { useCallback, useState } from 'react';
import { Box, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import { ScreenHeader } from '../access/AccessViews';
import SendingRecommendations from './SendingRecommendations';

export const NO_RECOMMENDATIONS = 'Cheaper routes for the numbers you fax and receiving lines you could share will appear here.';

type Section = 'sending';

export default function Recommendations({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  // null until a section has loaded (or when it could not load).
  const [counts, setCounts] = useState<Record<Section, number | null>>({ sending: null });
  const onSending = useCallback((count: number | null) => setCounts((known) => ({ ...known, sending: count })), []);
  const empty = Object.values(counts).every((count) => count === 0);

  return (
    <Box>
      <ScreenHeader title="Recommendations" />
      <Stack spacing={3}>
        <SendingRecommendations client={client} canWrite={canWrite} onCount={onSending} />
        {/* Receiving: ReceivingRecommendations goes here, with its own entry in `counts`. */}
      </Stack>
      {empty && (
        <Paper variant="outlined" sx={{ p: 3, borderRadius: 2 }}>
          <Typography variant="body1" color="text.secondary" data-testid="recommendations-empty">{NO_RECOMMENDATIONS}</Typography>
        </Paper>
      )}
    </Box>
  );
}
