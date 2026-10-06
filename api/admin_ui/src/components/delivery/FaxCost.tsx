// One fax's cost, in the carrier's words: "Telnyx charged $0.005 for this call." or "Cost not reported yet."
// The sentence comes from the server, which reads the carrier's reported charge and never shows an estimate as a charge.
import { useEffect, useState } from 'react';
import { Divider, ListItem, ListItemText, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { FaxCost } from '../../api/deliveryTypes';
import { formatMoneyList } from './shared';

// The cost line in the Jobs detail list; renders nothing for a fax that never placed a call.
export function FaxCostItem({ client, jobId }: { client: AdminAPIClient; jobId: string }) {
  const [cost, setCost] = useState<FaxCost | null>(null);
  useEffect(() => {
    let live = true;
    setCost(null);
    client.getFaxCost(jobId).then((value) => { if (live) setCost(value); }).catch(() => { if (live) setCost(null); });
    return () => { live = false; };
  }, [client, jobId]);
  if (!cost?.summary) return null;
  return (
    <>
      <Divider />
      <ListItem><ListItemText primary="Cost" secondary={cost.summary} /></ListItem>
    </>
  );
}

const REFRESH_MS = 60_000;

// Costs for the received faxes on screen, read in one request and refreshed once a minute.
export function useInboundCosts(client: AdminAPIClient, faxes: Array<{ id: string }>): Map<string, FaxCost> {
  const [costs, setCosts] = useState<Map<string, FaxCost>>(new Map());
  const ids = faxes.map((fax) => fax.id).filter(Boolean).slice(0, 100).join(',');
  useEffect(() => {
    if (!ids) {
      setCosts(new Map());
      return undefined;
    }
    let live = true;
    const read = () => client.getInboundCosts(ids.split(','))
      .then((result) => { if (live) setCosts(new Map(Object.entries(result.costs))); })
      .catch(() => undefined);
    void read();
    const timer = window.setInterval(() => { void read(); }, REFRESH_MS);
    return () => { live = false; window.clearInterval(timer); };
  }, [client, ids]);
  return costs;
}

// Costs for the sent faxes on screen, read in one request and refreshed once a minute.
export function useFaxCosts(client: AdminAPIClient, jobs: Array<{ id: string }>): Map<string, FaxCost> {
  const [costs, setCosts] = useState<Map<string, FaxCost>>(new Map());
  const ids = jobs.map((job) => job.id).filter(Boolean).slice(0, 100).join(',');
  useEffect(() => {
    if (!ids) {
      setCosts(new Map());
      return undefined;
    }
    let live = true;
    const read = () => client.getFaxCosts(ids.split(','))
      .then((result) => { if (live) setCosts(new Map(Object.entries(result.costs))); })
      .catch(() => undefined);
    void read();
    const timer = window.setInterval(() => { void read(); }, REFRESH_MS);
    return () => { live = false; window.clearInterval(timer); };
  }, [client, ids]);
  return costs;
}

// The short amount for a cost column: what was charged, or the estimate with the word
// "estimate" until the carrier reports. The full sentence stays in the cost's summary.
export function costAmount(cost: FaxCost | undefined): string {
  if (!cost || cost.state === 'none') return '-';
  if (cost.state === 'reported') return formatMoneyList(cost.reported_cost, '-');
  if (cost.state === 'partial') return `${formatMoneyList(cost.reported_cost, '-')} charged so far`;
  // The carrier priced part of the call and never the rest: never shown as the whole cost.
  if (cost.state === 'incomplete') return `${formatMoneyList(cost.reported_cost, '-')}, part never priced`;
  if (cost.state === 'included') return 'In your plan';
  // Delivered inside Faxbot to one of this installation's own numbers.
  if (cost.state === 'local') return 'No call';
  if (cost.state === 'unmatched') return 'Unknown';
  return cost.estimated_cost && cost.estimated_cost.length > 0
    ? `${formatMoneyList(cost.estimated_cost)} estimate` : 'Not reported yet';
}

export function InboundCostLine({ cost }: { cost: FaxCost | undefined }) {
  if (!cost?.summary) return null;
  return <Typography variant="caption" color="text.secondary" display="block">{cost.summary}</Typography>;
}
