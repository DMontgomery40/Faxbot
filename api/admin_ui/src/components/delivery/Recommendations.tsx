// Costs → Recommendations: ways to pay less, once Faxbot has some to offer.
// Each section loads its own recommendations and reports how many it shows; the
// empty sentence appears only when no section has anything to suggest yet.
import { useCallback, useState } from 'react';
import { Box, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { AdminDestination } from '../../navigation';
import { ScreenHeader } from '../access/AccessViews';
import { BillingStepsSection, FaxMarkerSection, PartnersSection, TollFreeSection } from './AdviceSections';
import { DiscoveryRecommendations } from './FindPartners';
import OtherCarriers from './OtherCarriers';
import PlanRecommendations from './PlanRecommendations';
import ReceivingRecommendations from './ReceivingRecommendations';
import RelayRecommendations from './RelayRecommendations';
import SendingRecommendations from './SendingRecommendations';
import FaxFriendlyRecommendation from './FaxFriendlyRecommendation';
import TrunkAdvice from './TrunkAdvice';
import NumberPlacement from './NumberPlacement';
import SiteAdvice from './SiteAdvice';

export const NO_RECOMMENDATIONS = 'Nothing to suggest yet. Cheaper routes for the numbers you fax will appear here.';

type Section = 'sending' | 'receiving' | 'plans' | 'carriers' | 'marker' | 'steps' | 'partners' | 'discovery'
  | 'tollFree' | 'pages' | 'relays' | 'trunks' | 'numbers' | 'sites';
const SECTIONS: Section[] = ['sending', 'receiving', 'plans', 'carriers', 'marker', 'steps', 'partners', 'discovery',
  'tollFree', 'pages', 'relays', 'trunks', 'numbers', 'sites'];

export default function Recommendations({ client, canWrite = false, onNavigate }: {
  client: AdminAPIClient;
  canWrite?: boolean;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  // null until a section has loaded (or when it could not load).
  const [counts, setCounts] = useState<Record<Section, number | null>>(
    Object.fromEntries(SECTIONS.map((section) => [section, null])) as Record<Section, number | null>);
  const report = useCallback((section: Section) => (count: number | null) => setCounts((known) => (
    known[section] === count ? known : { ...known, [section]: count })), []);
  const [callbacks] = useState(() => Object.fromEntries(SECTIONS.map((section) => [section, report(section)])) as
    Record<Section, (count: number | null) => void>);
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
        <SendingRecommendations client={client} canWrite={canWrite} onCount={callbacks.sending} onNavigate={onNavigate} />
        <ReceivingRecommendations client={client} onCount={callbacks.receiving} />
        <PlanRecommendations client={client} onCount={callbacks.plans} />
        <OtherCarriers client={client} onCount={callbacks.carriers} />
        <BillingStepsSection client={client} onCount={callbacks.steps} />
        <PartnersSection client={client} onCount={callbacks.partners} onNavigate={onNavigate} />
        <DiscoveryRecommendations client={client} onCount={callbacks.discovery} onNavigate={onNavigate} />
        <RelayRecommendations client={client} onCount={callbacks.relays} />
        <TollFreeSection client={client} onCount={callbacks.tollFree} onNavigate={onNavigate} />
        <FaxMarkerSection client={client} onCount={callbacks.marker} />
        <FaxFriendlyRecommendation client={client} onCount={callbacks.pages} />
        <TrunkAdvice client={client} onCount={callbacks.trunks} />
        <NumberPlacement client={client} canWrite={canWrite} onCount={callbacks.numbers} />
        <SiteAdvice client={client} canWrite={canWrite} onCount={callbacks.sites} />
      </Stack>
    </Box>
  );
}
