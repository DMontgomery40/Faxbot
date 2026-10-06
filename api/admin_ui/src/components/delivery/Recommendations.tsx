// Costs → Recommendations: ways to pay less, once Faxbot has some to offer.
// Each section loads its own recommendations and reports how many it shows; the
// empty sentence appears only when no section has anything to suggest yet.
import { useCallback, useState } from 'react';
import { Box, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import { ScreenHeader } from '../access/AccessViews';
import PlanRecommendations from './PlanRecommendations';
import ReceivingRecommendations from './ReceivingRecommendations';
import SendingRecommendations from './SendingRecommendations';

export const NO_RECOMMENDATIONS = 'Nothing to suggest yet. Cheaper routes for the numbers you fax will appear here.';

type Section = 'sending' | 'receiving' | 'plans';

export default function Recommendations({ client, canWrite = false }: { client: AdminAPIClient; canWrite?: boolean }) {
  // null until a section has loaded (or when it could not load).
  const [counts, setCounts] = useState<Record<Section, number | null>>({ sending: null, receiving: null, plans: null });
  const onSending = useCallback((count: number | null) => setCounts((known) => ({ ...known, sending: count })), []);
  const onReceiving = useCallback((count: number | null) => setCounts((known) => ({ ...known, receiving: count })), []);
  const onPlans = useCallback((count: number | null) => setCounts((known) => ({ ...known, plans: count })), []);
  const empty = Object.values(counts).every((count) => count === 0);

  return (
    <Box>
      <ScreenHeader title="Recommendations" />
      <Stack spacing={3}>
        {empty && (
          <Paper variant="outlined" sx={{ p: 3, borderRadius: 2 }}>
            <Typography variant="body1" color="text.secondary" data-testid="recommendations-empty">{NO_RECOMMENDATIONS}</Typography>
          </Paper>
        )}
        <SendingRecommendations client={client} canWrite={canWrite} onCount={onSending} />
        <ReceivingRecommendations client={client} onCount={onReceiving} />
        <PlanRecommendations client={client} onCount={onPlans} />
      </Stack>
    </Box>
  );
}
