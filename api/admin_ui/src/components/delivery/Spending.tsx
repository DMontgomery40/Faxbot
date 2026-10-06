// Spending per route: what the carrier charged, rate-card estimates only for faxes it has not billed yet,
// and how many are still waiting for the carrier's bill. One sentence per line, money as money.
import { useState } from 'react';
import { Box, Button, Card, CardContent, Grid, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { CarrierChargeStatus, Money, ProviderCosts, ReceivedCosts } from '../../api/deliveryTypes';
import { providerLabel } from '../../providerLabels';
import { DeliveryError, Notice, formatMinutes, formatMoney, formatMoneyList } from './shared';
import { NO_PUBLISHED_PRICE, notPriced, receivedCost, receivedLabel, sentCost, withCarrier } from './spendingSummary';
import { usePublishedPlans } from './RateCards';
import type { PublishedPlans } from '../../api/deliveryTypes';

function count(value: number, one: string, many = one === 'fax' ? 'faxes' : `${one}s`): string {
  return `${value} ${value === 1 ? one : many}`;
}

function charged(who: string | null | undefined, money: Money[], faxes: number, unit: string, unrecorded = 0): string {
  const extra = !unrecorded ? '' : unit === 'call' ? `, ${unrecorded} without a Faxbot call record`
    : ` and ${count(unrecorded, 'call')} without a Faxbot call record`;
  const items = unit === 'call' ? faxes + unrecorded : faxes;
  return `${who ?? 'Your provider'} charged ${formatMoneyList(money)} for ${count(items, unit)}${extra}.`;
}

function OpenCounts({ carrier, unreported, unpriced, estimate, awaiting, unmatched, unit }: {
  carrier: string | null | undefined;
  unreported: number;
  // Of unreported, those with no estimate either: never in the estimate or the total.
  unpriced: number;
  estimate: Money[] | undefined;
  awaiting: number;
  unmatched: number;
  unit: 'fax' | 'call';
}) {
  const bill = carrier ? `the ${carrier} bill` : "the carrier's bill";
  const estimated = unreported - unpriced;
  const open = notPriced(unpriced, unit);
  return (
    <>
      {estimated > 0 && estimate && estimate.length > 0 && (
        <Typography variant="body2" color="text.secondary">
          Estimated {formatMoneyList(estimate)} for {count(estimated, unit)} not billed yet.
        </Typography>
      )}
      {open && (
        <Typography variant="body2" color="text.secondary" data-testid={`not-priced-${unit}`}>
          {`${open.charAt(0).toUpperCase()}${open.slice(1)}.`}
        </Typography>
      )}
      {awaiting > 0 && (
        <Typography variant="body2" color="text.secondary">{count(awaiting, unit)} waiting for {bill}.</Typography>
      )}
      {unmatched > 0 && (
        <Typography variant="body2" color="warning.main">
          {count(unmatched, unit)} could not be matched to one {carrier ?? 'carrier'} record, so {unmatched === 1 ? 'its cost is' : 'their cost is'} unknown.
        </Typography>
      )}
    </>
  );
}

function caption(reported: Money[], estimatedOpen: Money[] | undefined, unreported: number): string {
  const open = unreported > 0 && (estimatedOpen?.length ?? 0) > 0;
  if (reported.length > 0) return open ? 'charged and estimated' : 'charged';
  return open ? 'estimated' : '';
}

function Unrecorded({ carrier, calls, cost, matched }: { carrier: string | null | undefined; calls: number; cost: Money[] | undefined; matched: number }) {
  if (!calls) return null;
  const who = carrier ?? 'Your carrier';
  const unmatched = calls - matched;
  return (
    <>
      {unmatched > 0 && (
        <Typography variant="body2" color="text.secondary">
          {who} billed {count(unmatched, 'call')} Faxbot has no record of: {formatMoneyList(cost)}.
        </Typography>
      )}
      {matched > 0 && (
        <Typography variant="body2" color="text.secondary">
          {count(matched, 'call')} came in that Faxbot did not record at the time; {matched === 1 ? 'its fax is' : 'their faxes are'} in Received.
        </Typography>
      )}
    </>
  );
}

// published: what the provider publishes when its API has no published price (eFax), from Rate cards.
function SentCard({ provider, published }: { provider: ProviderCosts; published?: PublishedPlans }) {
  const reported = provider.attempts_with_reported_cost ?? 0;
  const top = { amount: sentCost(provider),
    caption: provider.plan ? '' : caption(provider.reported_cost, provider.estimated_cost_not_reported, provider.attempts_without_reported_cost) };
  const name = providerLabel(provider.provider_id);
  return (
    <Card variant="outlined" sx={{ borderRadius: 2, height: '100%' }}>
      <CardContent>
        <Typography variant="subtitle1">{withCarrier(name, provider.carrier)}</Typography>
        <Typography variant="h5" component="p" sx={{ mt: 1 }}>{top.amount}</Typography>
        {top.caption && <Typography variant="caption" color="text.secondary">{top.caption}</Typography>}

        {published && top.amount === NO_PUBLISHED_PRICE && (
          <Typography variant="body2" color="text.secondary" data-testid={`published-note-${provider.provider_id}`}>
            {published.sentence}{published.card ? ' Prices & plans can use it as your estimate.' : ''}
          </Typography>
        )}
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          {count(provider.attempts, 'fax', 'faxes')}, {provider.successes} delivered, {formatMinutes(provider.billed_minutes)}, {count(provider.billed_pages, 'page')}
        </Typography>
        {(reported > 0 || (provider.unrecorded_calls ?? 0) > 0) && (
          <Typography variant="body2" color="text.secondary">
            {charged(provider.carrier, provider.reported_cost, reported, 'fax', provider.unrecorded_calls ?? 0)}
          </Typography>
        )}
        {provider.plan?.period_fee && (
          <Typography variant="body2" color="text.secondary">
            Counted in the total: {formatMoney(provider.plan.period_fee)} for these {provider.plan.period_days ?? 30} days (the monthly fee, pro-rated by day).
          </Typography>
        )}
        <Unrecorded carrier={provider.carrier} calls={provider.unrecorded_calls ?? 0}
          cost={provider.unrecorded_unmatched_cost ?? provider.unrecorded_cost} matched={provider.unrecorded_matched_to_faxes ?? 0} />
        {!provider.plan && (
          <OpenCounts carrier={provider.carrier} unreported={provider.attempts_without_reported_cost}
            // A route with no published price says so above; its faxes are not counted again.
            unpriced={top.amount === NO_PUBLISHED_PRICE ? 0 : provider.attempts_not_priced ?? 0}
            estimate={provider.estimated_cost_not_reported} awaiting={provider.awaiting_carrier_bill ?? 0}
            unmatched={provider.unmatched_charges ?? 0} unit="fax" />
        )}
      </CardContent>
    </Card>
  );
}

function ReceivedCard({ entry }: { entry: ReceivedCosts }) {
  const top = { amount: receivedCost(entry),
    caption: caption(entry.reported_cost, entry.estimated_cost_not_reported, entry.calls_without_reported_cost) };
  return (
    <Card variant="outlined" sx={{ borderRadius: 2, height: '100%' }}>
      <CardContent>
        <Typography variant="subtitle1">{receivedLabel(entry)}</Typography>
        <Typography variant="h5" component="p" sx={{ mt: 1 }}>{top.amount}</Typography>
        {top.caption && <Typography variant="caption" color="text.secondary">{top.caption}</Typography>}
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          {count(entry.calls + (entry.unrecorded_calls ?? 0), 'call')}, {count(entry.faxes + (entry.unrecorded_matched_to_faxes ?? 0), 'fax', 'faxes')} received, {formatMinutes(entry.billed_minutes)}
        </Typography>
        {(entry.calls_with_reported_cost > 0 || (entry.unrecorded_calls ?? 0) > 0) && (
          <Typography variant="body2" color="text.secondary">
            {charged(entry.carrier, entry.reported_cost, entry.calls_with_reported_cost, 'call', entry.unrecorded_calls ?? 0)}
          </Typography>
        )}
        <OpenCounts carrier={entry.carrier} unreported={entry.calls_without_reported_cost}
          unpriced={entry.calls_not_priced ?? 0}
          estimate={entry.estimated_cost_not_reported} awaiting={entry.awaiting_carrier_bill}
          unmatched={entry.unmatched_charges} unit="call" />
        <Unrecorded carrier={entry.carrier} calls={entry.unrecorded_calls ?? 0}
          cost={entry.unrecorded_unmatched_cost ?? entry.unrecorded_cost} matched={entry.unrecorded_matched_to_faxes ?? 0} />
      </CardContent>
    </Card>
  );
}

export default function Spending({ client, providers, received, carrier, canWrite, onChanged }: {
  client: AdminAPIClient;
  providers: ProviderCosts[];
  received: ReceivedCosts[];
  carrier: CarrierChargeStatus | null;
  canWrite: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const published = usePublishedPlans(client, []);

  const checkNow = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await client.reconcileCharges();
      setNotice(result.summary);
      onChanged();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Notice message={notice} onClose={() => setNotice(null)} />
      {carrier?.supported && !carrier.readable && (
        <Typography variant="body2" color="text.secondary" mb={2}>
          {carrier.carrier} call charges appear here once a {carrier.carrier} API key is added to .env.
        </Typography>
      )}
      {carrier?.supported && carrier.readable && canWrite && (
        <Box mb={2}>
          <Button variant="outlined" onClick={() => void checkNow()} disabled={busy} sx={{ borderRadius: 2 }}>
            {busy ? 'Checking…' : `Check ${carrier.carrier} charges now`}
          </Button>
        </Box>
      )}
      {providers.length === 0 && received.length === 0 ? (
        <Typography color="text.secondary">No faxes have been sent or received in the last 30 days.</Typography>
      ) : (
        <Grid container spacing={2}>
          {providers.map((provider) => (
            <Grid item xs={12} sm={6} md={4} key={`sent-${provider.provider_id}`}>
              <SentCard provider={provider} published={published.find((item) => item.provider_id === provider.provider_id)} />
            </Grid>
          ))}
          {received.map((entry) => (
            <Grid item xs={12} sm={6} md={4} key={`received-${entry.carrier ?? entry.provider_id}`}>
              <ReceivedCard entry={entry} />
            </Grid>
          ))}
        </Grid>
      )}
    </Box>
  );
}
