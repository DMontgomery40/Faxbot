// Delivery routes: what faxing costs by provider, each fax number's routes and
// reliability, the prices Faxbot uses, and direct delivery partners. A console
// page can show one of these parts on its own.
import { useCallback, useEffect, useState } from 'react';
import { Box, Typography } from '@mui/material';
import AdminAPIClient from '../api/client';
import type { CarrierChargeStatus, Destination, DirectPartner, ProviderCosts, RateCard, ReceivedCosts, ReceivedFaxCosts, TollFreeTerms } from '../api/deliveryTypes';
import { LoadStateView, ScreenHeader, loadFailure, type LoadState } from './access/AccessViews';
import Destinations from './delivery/Destinations';
import DirectPartners from './delivery/DirectPartners';
import FindPartners from './delivery/FindPartners';
import PlanBudgets from './delivery/PlanBudgets';
import PlanAllocation from './delivery/PlanAllocation';
import RateCards from './delivery/RateCards';
import TollFreePrices from './delivery/TollFreePrices';
import Spending from './delivery/Spending';
import RelayCosts from './delivery/RelayCosts';
import LoadFailed, { saysFailure } from './common/LoadFailed';

export type DeliveryRoutesSection = 'spending' | 'numbers' | 'rates' | 'partners';

const SECTIONS: Record<DeliveryRoutesSection, { title: string; text: string }> = {
  spending: { title: 'Spending', text: 'What your carriers charged over the last 30 days, with rate-card estimates for faxes they have not billed yet.' },
  numbers: { title: 'Fax numbers', text: 'How each number has been reached and what it cost.' },
  rates: { title: 'Prices & plans', text: 'Advertised prices Faxbot uses to estimate costs and choose the cheapest route.' },
  partners: { title: 'Direct partners', text: 'Organizations that receive your documents directly, with no fax call.' },
};

// The page title and sentence when one part is shown on its own page.
const PAGES: Record<DeliveryRoutesSection, { title: string; text: string }> = {
  spending: { title: 'Spending', text: SECTIONS.spending.text },
  numbers: { title: 'Recipients', text: 'The fax numbers you send to and what each one cost.' },
  rates: { title: 'Prices & plans', text: SECTIONS.rates.text },
  partners: { title: 'Partners', text: SECTIONS.partners.text },
};

function Section({ title, text, children }: { title: string; text: string; children: React.ReactNode }) {
  return (
    <Box component="section" mb={4}>
      <Typography variant="h6" component="h2">{title}</Typography>
      <Typography variant="body2" color="text.secondary" mb={2}>{text}</Typography>
      {children}
    </Box>
  );
}

export default function DeliveryRoutes({ client, canWrite, section }: { client: AdminAPIClient; canWrite: boolean; section?: DeliveryRoutesSection }) {
  const [state, setState] = useState<LoadState>('loading');
  const [destinations, setDestinations] = useState<Destination[]>([]);
  const [providers, setProviders] = useState<ProviderCosts[]>([]);
  const [received, setReceived] = useState<ReceivedCosts[]>([]);
  const [receivedFaxes, setReceivedFaxes] = useState<ReceivedFaxCosts[]>([]);
  const [carrier, setCarrier] = useState<CarrierChargeStatus | null>(null);
  const [cards, setCards] = useState<RateCard[]>([]);
  const [tollFree, setTollFree] = useState<TollFreeTerms[]>([]);
  const [partners, setPartners] = useState<DirectPartner[]>([]);
  // Numbers whose recipient runs Faxbot (Partners → Find partners), for the recipients list.
  const [suggested, setSuggested] = useState<string[] | null>(null);
  // Partners or suggestions that could not be read are said where they would be (quiet without permission).
  const [unread, setUnread] = useState<{ partners: boolean; suggestions: boolean }>({ partners: false, suggestions: false });
  const shows = (part: DeliveryRoutesSection) => !section || section === part;

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    const wants = (part: DeliveryRoutesSection) => !section || section === part;
    const failed = { partners: false, suggestions: false };
    const noted = (part: keyof typeof failed) => (failure: unknown) => {
      failed[part] = saysFailure(failure);
      return null;
    };
    try {
      const [routes, costs, rates, peers, discovery] = await Promise.all([
        wants('numbers') ? client.listDestinations() : null,
        wants('spending') ? client.getRouteCosts() : null,
        wants('rates') ? client.listRateCards() : null,
        // The recipients list names the partner each number belongs to, when partners can be read.
        wants('partners') || section === 'numbers' ? client.listDirectPartners().catch(noted('partners')) : null,
        section === 'numbers' ? client.getDiscovery().catch(noted('suggestions')) : null,
      ]);
      setUnread(failed);
      if (routes) setDestinations(routes.destinations);
      if (costs) {
        setProviders(costs.providers);
        setReceived(costs.received ?? []);
        setReceivedFaxes(costs.received_faxes ?? []);
        setCarrier(costs.carrier_charges ?? null);
      }
      if (rates) {
        setCards(rates.cards);
        setTollFree(rates.toll_free ?? []);
      }
      if (peers) setPartners(peers.peers);
      if (discovery) setSuggested(discovery.suggestions.map((item) => item.number));
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client, section]);

  useEffect(() => { void load(); }, [load]);

  const header = section
    ? <ScreenHeader title={PAGES[section].title} onRefresh={() => void load()} busy={state === 'loading'} subtitle={PAGES[section].text} />
    : <ScreenHeader title="Delivery routes" onRefresh={() => void load()} busy={state === 'loading'}
      subtitle="What sending costs, which route each fax number uses, and partners who receive documents directly." />;
  // On its own page a part needs no second heading.
  const part = (key: DeliveryRoutesSection, children: React.ReactNode) => (section
    ? <Box component="section" mb={4}>{children}</Box>
    : <Section title={SECTIONS[key].title} text={SECTIONS[key].text}>{children}</Section>);

  return (
    <Box>
      {header}
      {state !== 'ready' ? <LoadStateView state={state} onRetry={() => void load()} /> : (
        <>
          {shows('spending') && part('spending',
            <>
              <Spending client={client} providers={providers} received={received} receivedFaxes={receivedFaxes}
                carrier={carrier} canWrite={canWrite}
                onChanged={() => void load()} />
              <RelayCosts client={client} />
            </>)}
          {shows('numbers') && part('numbers',
            <>
              {section === 'numbers' && unread.partners && <LoadFailed testId="numbers-partners-unread"
                text="Your direct partners could not be loaded, so numbers are shown without them. Try again." />}
              {section === 'numbers' && unread.suggestions && <LoadFailed testId="numbers-suggestions-unread"
                text="Numbers whose recipient runs Faxbot could not be loaded. Try again." />}
              <Destinations client={client} destinations={destinations} canWrite={canWrite} onChanged={() => void load()}
                partners={section === 'numbers' ? partners : null} suggested={section === 'numbers' ? suggested : null} />
            </>)}
          {shows('rates') && part('rates',
            <>
              <RateCards client={client} cards={cards} canWrite={canWrite} onChanged={() => void load()} />
              <TollFreePrices items={tollFree} />
              <PlanBudgets client={client} canWrite={canWrite} />
              <PlanAllocation client={client} />
            </>)}
          {shows('partners') && part('partners',
            <>
              {unread.partners
                ? <LoadFailed testId="partners-unread" text="Your direct partners could not be loaded. Try again." />
                : <DirectPartners client={client} partners={partners} canWrite={canWrite} onChanged={() => void load()} />}
              <Box component="section" mt={4}>
                <Typography variant="h6" component="h2">Find partners</Typography>
                <Typography variant="body2" color="text.secondary" mb={2}>
                  Recipients that run Faxbot, found from your fax calls, your partners' introductions and directories you trust.
                </Typography>
                <FindPartners client={client} canWrite={canWrite} onChanged={() => void load()} />
              </Box>
            </>)}
        </>
      )}
    </Box>
  );
}
