// One fax's cost, in the carrier's words: "Telnyx charged $0.005 for this call." or "Cost not reported yet."
// The sentence comes from the server, which reads the carrier's reported charge and never shows an estimate as a charge.
import { useEffect, useState } from 'react';
import { Divider, ListItem, ListItemText, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { FaxCost } from '../../api/deliveryTypes';
import { formatMoneyList } from './shared';
import LoadFailed, { saysFailure } from '../common/LoadFailed';

// The costs read for a list: ``failed`` when the last read failed (said once above the list; quiet without
// permission). A fax without a cost is simply absent.
export type FaxCosts = Map<string, FaxCost> & { failed?: boolean };

function costMap(entries: Record<string, FaxCost> | Map<string, FaxCost>, failed = false): FaxCosts {
  return Object.assign(new Map(entries instanceof Map ? entries : Object.entries(entries)), { failed });
}

// One sentence above a list whose costs could not be read.
export function CostsUnread({ costs }: { costs: FaxCosts }) {
  return costs.failed ? <LoadFailed testId="costs-unread" text="Costs could not be loaded. Try again." /> : null;
}

// The cost line in the Jobs detail list; renders nothing for a fax that never placed a call.
export function FaxCostItem({ client, jobId }: { client: AdminAPIClient; jobId: string }) {
  const [cost, setCost] = useState<FaxCost | null>(null);
  const [unread, setUnread] = useState(false);
  useEffect(() => {
    let live = true;
    setCost(null);
    setUnread(false);
    client.getFaxCost(jobId).then((value) => { if (live) setCost(value); }).catch((failure) => {
      if (!live) return;
      setCost(null);
      setUnread(saysFailure(failure));
    });
    return () => { live = false; };
  }, [client, jobId]);
  if (!cost?.summary && !unread) return null;
  return (
    <>
      <Divider />
      <ListItem><ListItemText primary="Cost" secondary={cost?.summary
        ?? <LoadFailed testId="cost-unread" text="The cost could not be loaded. Try again." />} /></ListItem>
    </>
  );
}

const REFRESH_MS = 60_000;

// Costs for the received faxes on screen, read in one request and refreshed once a minute.
export function useInboundCosts(client: AdminAPIClient, faxes: Array<{ id: string }>): FaxCosts {
  const [costs, setCosts] = useState<FaxCosts>(costMap({}));
  const ids = faxes.map((fax) => fax.id).filter(Boolean).slice(0, 100).join(',');
  useEffect(() => {
    if (!ids) {
      setCosts(costMap({}));
      return undefined;
    }
    let live = true;
    const read = () => client.getInboundCosts(ids.split(','))
      .then((result) => { if (live) setCosts(costMap(result.costs)); })
      // The costs already shown stay; a failure is said once above the list.
      .catch((failure) => { if (live && saysFailure(failure)) setCosts((current) => costMap(current, true)); });
    void read();
    const timer = window.setInterval(() => { void read(); }, REFRESH_MS);
    return () => { live = false; window.clearInterval(timer); };
  }, [client, ids]);
  return costs;
}

// Costs for the sent faxes on screen, read in one request and refreshed once a minute.
export function useFaxCosts(client: AdminAPIClient, jobs: Array<{ id: string }>): FaxCosts {
  const [costs, setCosts] = useState<FaxCosts>(costMap({}));
  const ids = jobs.map((job) => job.id).filter(Boolean).slice(0, 100).join(',');
  useEffect(() => {
    if (!ids) {
      setCosts(costMap({}));
      return undefined;
    }
    let live = true;
    const read = () => client.getFaxCosts(ids.split(','))
      .then((result) => { if (live) setCosts(costMap(result.costs)); })
      // The costs already shown stay; a failure is said once above the list.
      .catch((failure) => { if (live && saysFailure(failure)) setCosts((current) => costMap(current, true)); });
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
