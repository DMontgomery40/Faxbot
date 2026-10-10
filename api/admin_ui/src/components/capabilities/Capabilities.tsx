// Savings & optimization → Capabilities: everything Faxbot can do, grouped by the outcome it serves, from
// GET /routing/capabilities. The address carries the view: ?show=ready filters the list, ?key=<key> opens one
// capability, so Back, reload and a copied link all return to it. Never money: amounts stay on Savings.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, CircularProgress, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../../api/client';
import type { Capabilities as CapabilitiesView } from '../../api/capabilityTypes';
import { ScreenHeader } from '../access/AccessViews';
import { DeliveryError } from '../delivery/shared';
import CapabilityList from './CapabilityList';
import CapabilityPage from './CapabilityPage';
import { AddressLink, CAPABILITIES_ADDRESS, readCapabilityFilter, type Navigate } from './text';

export default function Capabilities({ client, show = null, capability = null, onNavigate }: {
  client: AdminAPIClient;
  // The address's ?show= and ?key=.
  show?: string | null;
  capability?: string | null;
  onNavigate?: Navigate;
}) {
  const [data, setData] = useState<CapabilitiesView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setData(await client.getCapabilities());
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const denied = error instanceof AdminAPIError && (error.status === 401 || error.status === 403);
  const found = capability && data ? data.outcomes.flatMap((outcome) => outcome.capabilities
    .filter((item) => item.key === capability).map((item) => ({ item, outcome }))) : [];

  if (capability && data) {
    if (found.length === 0) {
      return (
        <Box data-testid="capability-unknown">
          <ScreenHeader title={data.title} />
          <Typography variant="body1" sx={{ mb: 1 }}>This capability is not in the list.</Typography>
          <AddressLink address={CAPABILITIES_ADDRESS} onNavigate={onNavigate}>All capabilities</AddressLink>
        </Box>
      );
    }
    return <CapabilityPage item={found[0].item} outcome={found[0].outcome} onNavigate={onNavigate} />;
  }

  return (
    <Box>
      <ScreenHeader title={data?.title ?? 'Capabilities'} subtitle={data?.sentence} onRefresh={() => void load()} busy={busy} />
      {denied && (
        <Alert severity="info" data-testid="capabilities-denied" sx={{ mb: 3, borderRadius: 2 }}>
          Capabilities need permission to read settings, which your role does not include.
        </Alert>
      )}
      {!denied && <DeliveryError error={error} onClose={() => setError(null)} />}
      {!data && busy && <CircularProgress size={24} aria-label="Loading capabilities" />}
      {data && <CapabilityList data={data} show={readCapabilityFilter(show)} onNavigate={onNavigate} />}
    </Box>
  );
}
