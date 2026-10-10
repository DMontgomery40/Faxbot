// Savings & optimization → Opportunities: ways to pay less, once Faxbot has some to offer.
// Each section loads its own recommendations and reports how many it shows; the
// empty sentence appears only when no section has anything to suggest yet. Each
// section has an anchor (#/costs/recommendations?section=plans): the Overview's
// savings map links to it, and the section opened that way is outlined.
import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Box, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import { AnalysisCard } from '../AIAnalysis';
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
import FaxServerRenewal from './FaxServerRenewal';

export const NO_RECOMMENDATIONS = 'Nothing to suggest yet. Cheaper routes for the numbers you fax will appear here.';

type Section = 'sending' | 'receiving' | 'plans' | 'carriers' | 'marker' | 'steps' | 'partners' | 'discovery'
  | 'tollFree' | 'pages' | 'relays' | 'trunks' | 'numbers' | 'sites' | 'renewal';
const SECTIONS: Section[] = ['sending', 'receiving', 'plans', 'carriers', 'marker', 'steps', 'partners', 'discovery',
  'tollFree', 'pages', 'relays', 'trunks', 'numbers', 'sites', 'renewal'];

// Each section's name, for the line that stands in for a section the address names that has nothing yet; the
// same names as the savings map's advice cards.
const SECTION_TITLES: Record<Section, string> = {
  sending: 'Cheaper routes per number', receiving: 'Receiving lines and numbers', plans: 'Plans worth their fee',
  carriers: "Other carriers' prices", marker: 'Fax marker on calls', steps: 'Calls just past a billed minute',
  partners: 'Partner candidates', discovery: 'Recipients that run Faxbot', tollFree: 'Toll-free numbers on file',
  pages: 'Time lighter shading would save', relays: 'Partners that could relay', trunks: 'Your trunks compared',
  numbers: 'Where each number should live', sites: 'Calls by state', renewal: 'Fax server renewal',
};

// The element id of one section's anchor.
export function recommendationSectionId(section: string): string {
  return `recommendations-${section}`;
}

// One section with its anchor: the section the address names is outlined, and says so when it has nothing yet.
function Anchor({ section, focus, count, children }: {
  section: Section; focus: string | null; count: number | null; children: ReactNode;
}) {
  const focused = focus === section;
  return (
    <Box id={recommendationSectionId(section)} data-testid={recommendationSectionId(section)}
      data-focused={focused ? 'true' : undefined}
      sx={{ scrollMarginTop: 80, '&:empty': { display: 'none' }, // a section with nothing to show takes no room
        ...(focused ? { outline: 2, outlineColor: 'primary.main', outlineOffset: 6, borderRadius: 2 } : {}) }}>
      {children}
      {focused && count === 0 && (
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
          <Typography variant="body2" color="text.secondary">{SECTION_TITLES[section]}: nothing to suggest yet.</Typography>
        </Paper>
      )}
    </Box>
  );
}

export default function Recommendations({ client, canWrite = false, onNavigate, focus = null }: {
  client: AdminAPIClient;
  canWrite?: boolean;
  onNavigate?: (destination: AdminDestination) => void;
  // The section the address names, from the Overview's savings map.
  focus?: string | null;
}) {
  // null until a section has loaded (or when it could not load).
  const [counts, setCounts] = useState<Record<Section, number | null>>(
    Object.fromEntries(SECTIONS.map((section) => [section, null])) as Record<Section, number | null>);
  const report = useCallback((section: Section) => (count: number | null) => setCounts((known) => (
    known[section] === count ? known : { ...known, [section]: count })), []);
  const [callbacks] = useState(() => Object.fromEntries(SECTIONS.map((section) => [section, report(section)])) as
    Record<Section, (count: number | null) => void>);
  const empty = Object.values(counts).every((count) => count === 0);
  const focusLoaded = focus !== null && SECTIONS.includes(focus as Section) && counts[focus as Section] !== null;

  // The section the address names comes into view once it has loaded.
  useEffect(() => {
    if (!focus || !focusLoaded) return;
    document.getElementById(recommendationSectionId(focus))?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  }, [focus, focusLoaded]);

  return (
    <Box>
      <ScreenHeader title="Opportunities" />
      <AnalysisCard client={client} onNavigate={onNavigate} />
      <Stack spacing={3}>
        {empty && (
          <Paper variant="outlined" sx={{ p: 3, borderRadius: 2 }}>
            <Typography variant="body1" color="text.secondary" data-testid="recommendations-empty">{NO_RECOMMENDATIONS}</Typography>
          </Paper>
        )}
        <Anchor section="sending" focus={focus} count={counts.sending}>
          <SendingRecommendations client={client} canWrite={canWrite} onCount={callbacks.sending} onNavigate={onNavigate} />
        </Anchor>
        <Anchor section="receiving" focus={focus} count={counts.receiving}>
          <ReceivingRecommendations client={client} onCount={callbacks.receiving} />
        </Anchor>
        <Anchor section="plans" focus={focus} count={counts.plans}>
          <PlanRecommendations client={client} onCount={callbacks.plans} />
        </Anchor>
        <Anchor section="carriers" focus={focus} count={counts.carriers}>
          <OtherCarriers client={client} onCount={callbacks.carriers} />
        </Anchor>
        <Anchor section="steps" focus={focus} count={counts.steps}>
          <BillingStepsSection client={client} onCount={callbacks.steps} />
        </Anchor>
        <Anchor section="partners" focus={focus} count={counts.partners}>
          <PartnersSection client={client} onCount={callbacks.partners} onNavigate={onNavigate} />
        </Anchor>
        <Anchor section="discovery" focus={focus} count={counts.discovery}>
          <DiscoveryRecommendations client={client} onCount={callbacks.discovery} onNavigate={onNavigate} />
        </Anchor>
        <Anchor section="relays" focus={focus} count={counts.relays}>
          <RelayRecommendations client={client} onCount={callbacks.relays} />
        </Anchor>
        <Anchor section="tollFree" focus={focus} count={counts.tollFree}>
          <TollFreeSection client={client} onCount={callbacks.tollFree} onNavigate={onNavigate} />
        </Anchor>
        <Anchor section="marker" focus={focus} count={counts.marker}>
          <FaxMarkerSection client={client} onCount={callbacks.marker} />
        </Anchor>
        <Anchor section="pages" focus={focus} count={counts.pages}>
          <FaxFriendlyRecommendation client={client} onCount={callbacks.pages} />
        </Anchor>
        <Anchor section="trunks" focus={focus} count={counts.trunks}>
          <TrunkAdvice client={client} onCount={callbacks.trunks} />
        </Anchor>
        <Anchor section="numbers" focus={focus} count={counts.numbers}>
          <NumberPlacement client={client} onCount={callbacks.numbers} />
        </Anchor>
        <Anchor section="sites" focus={focus} count={counts.sites}>
          <SiteAdvice client={client} canWrite={canWrite} onCount={callbacks.sites} />
        </Anchor>
        <Anchor section="renewal" focus={focus} count={counts.renewal}>
          <FaxServerRenewal client={client} canWrite={canWrite} onCount={callbacks.renewal} />
        </Anchor>
      </Stack>
    </Box>
  );
}
