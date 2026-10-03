// Delivery routes: what faxing costs by provider, each fax number's routes and
// reliability, the prices Faxbot uses, and direct delivery partners.
import { useCallback, useEffect, useState } from 'react';
import { Box, Card, CardContent, Grid, Typography } from '@mui/material';
import AdminAPIClient from '../api/client';
import type { Destination, DirectPartner, ProviderCosts, RateCard } from '../api/deliveryTypes';
import { LoadStateView, ScreenHeader, loadFailure, type LoadState } from './access/AccessViews';
import Destinations from './delivery/Destinations';
import DirectPartners from './delivery/DirectPartners';
import RateCards from './delivery/RateCards';
import { formatMinutes, formatMoneyList } from './delivery/shared';

function Section({ title, text, children }: { title: string; text: string; children: React.ReactNode }) {
  return (
    <Box component="section" mb={4}>
      <Typography variant="h6" component="h2">{title}</Typography>
      <Typography variant="body2" color="text.secondary" mb={2}>{text}</Typography>
      {children}
    </Box>
  );
}

function Spending({ providers }: { providers: ProviderCosts[] }) {
  if (providers.length === 0) {
    return <Typography color="text.secondary">No faxes have been sent in the last 30 days.</Typography>;
  }
  return (
    <Grid container spacing={2}>
      {providers.map((provider) => (
        <Grid item xs={12} sm={6} md={4} key={provider.provider_id}>
          <Card variant="outlined" sx={{ borderRadius: 2, height: '100%' }}>
            <CardContent>
              <Typography variant="subtitle1">{provider.label}</Typography>
              <Typography variant="h5" component="p" sx={{ my: 1 }}>{formatMoneyList(provider.estimated_cost, 'No price set')}</Typography>
              <Typography variant="body2" color="text.secondary">
                {provider.attempts} {provider.attempts === 1 ? 'fax' : 'faxes'}, {provider.successes} delivered, {formatMinutes(provider.billed_minutes)}, {provider.billed_pages} {provider.billed_pages === 1 ? 'page' : 'pages'}
              </Typography>
              {provider.reported_cost.length > 0 && (
                <Typography variant="body2" color="text.secondary">Charged by the provider: {formatMoneyList(provider.reported_cost)}</Typography>
              )}
            </CardContent>
          </Card>
        </Grid>
      ))}
    </Grid>
  );
}

export default function DeliveryRoutes({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [state, setState] = useState<LoadState>('loading');
  const [destinations, setDestinations] = useState<Destination[]>([]);
  const [providers, setProviders] = useState<ProviderCosts[]>([]);
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
          <Section title="Spending" text="Estimated from your rate cards for the last 30 days.">
            <Spending providers={providers} />
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
