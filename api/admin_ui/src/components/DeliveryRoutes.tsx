// Delivery routes: what faxing costs by provider, each fax number's routes and
// reliability, the prices Faxbot uses, and direct delivery partners.
import { useCallback, useEffect, useState } from 'react';
import { Box, Typography } from '@mui/material';
import AdminAPIClient from '../api/client';
import type { CarrierChargeStatus, Destination, DirectPartner, ProviderCosts, RateCard, ReceivedCosts } from '../api/deliveryTypes';
import { LoadStateView, ScreenHeader, loadFailure, type LoadState } from './access/AccessViews';
import Destinations from './delivery/Destinations';
import DirectPartners from './delivery/DirectPartners';
import RateCards from './delivery/RateCards';
import Spending from './delivery/Spending';

function Section({ title, text, children }: { title: string; text: string; children: React.ReactNode }) {
  return (
    <Box component="section" mb={4}>
      <Typography variant="h6" component="h2">{title}</Typography>
      <Typography variant="body2" color="text.secondary" mb={2}>{text}</Typography>
      {children}
    </Box>
  );
}

export default function DeliveryRoutes({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [state, setState] = useState<LoadState>('loading');
  const [destinations, setDestinations] = useState<Destination[]>([]);
  const [providers, setProviders] = useState<ProviderCosts[]>([]);
  const [received, setReceived] = useState<ReceivedCosts[]>([]);
  const [carrier, setCarrier] = useState<CarrierChargeStatus | null>(null);
  const [cards, setCards] = useState<RateCard[]>([]);
  const [partners, setPartners] = useState<DirectPartner[]>([]);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      const [routes, costs, rates, peers] = await Promise.all([
        client.listDestinations(), client.getRouteCosts(), client.listRateCards(), client.listDirectPartners(),
      ]);
      setDestinations(routes.destinations);
      setProviders(costs.providers);
      setReceived(costs.received ?? []);
      setCarrier(costs.carrier_charges ?? null);
      setCards(rates.cards);
      setPartners(peers.peers);
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Box>
      <ScreenHeader title="Delivery routes" onRefresh={() => void load()} busy={state === 'loading'}
        subtitle="What sending costs, which route each fax number uses, and partners who receive documents directly." />
      {state !== 'ready' ? <LoadStateView state={state} onRetry={() => void load()} /> : (
        <>
          <Section title="Spending" text="What your carriers charged over the last 30 days, with rate-card estimates for faxes they have not billed yet.">
            <Spending client={client} providers={providers} received={received} carrier={carrier} canWrite={canWrite}
              onChanged={() => void load()} />
          </Section>
          <Section title="Fax numbers" text="How each number has been reached and what it cost.">
            <Destinations client={client} destinations={destinations} canWrite={canWrite} onChanged={() => void load()} />
          </Section>
          <Section title="Rate cards" text="Advertised prices Faxbot uses to estimate costs and choose the cheapest route.">
            <RateCards client={client} cards={cards} canWrite={canWrite} onChanged={() => void load()} />
          </Section>
          <Section title="Direct partners" text="Organizations that receive your documents directly, with no fax call.">
            <DirectPartners client={client} partners={partners} canWrite={canWrite} onChanged={() => void load()} />
          </Section>
        </>
      )}
    </Box>
  );
}
