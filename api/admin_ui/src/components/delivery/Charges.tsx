// Costs → Charges: how Faxbot reads what each account charged, what Sinch and Phaxio charged for received faxes,
// and faxes a provider billed that Faxbot has no record of. Faxbot only lists those faxes: it never sends, fetches
// or changes one. "Check now" lists the accounts' faxes at their providers again.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Link, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { ChargesView } from '../../api/chargesTypes';
import { formatServerTime } from '../../api/time';
import { LoadStateView, ScreenHeader, loadFailure, type LoadState } from '../access/AccessViews';
import { DeliveryError, Notice, formatMoney } from './shared';

const OUTCOME: Record<string, string> = {
  complete: 'Checked',
  partial: 'Too many faxes to check at once',
  unavailable: 'Could not be reached',
  unsupported: 'Publishes no list',
};

export function lastChecked(source: ChargesView['accounts'][number]): string {
  if (!source.listing) return '-';
  if (!source.last_checked) return 'Not checked yet';
  const when = formatServerTime(source.last_checked);
  return source.last_outcome && source.last_outcome !== 'complete'
    ? `${when}: ${OUTCOME[source.last_outcome] ?? ''}` : when;
}

export default function Charges({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [state, setState] = useState<LoadState>('loading');
  const [data, setData] = useState<ChargesView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      setData(await client.getCharges());
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const checkNow = async () => {
    setChecking(true);
    setError(null);
    try {
      const result = await client.sweepCharges(null, 7);
      setNotice(result.summary);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setChecking(false);
    }
  };

  const listing = (data?.accounts ?? []).some((source) => source.listing);
  return (
    <Box>
      <ScreenHeader title="Charges" onRefresh={() => void load()} busy={state === 'loading'}
        subtitle="What your providers charged, and faxes they billed that Faxbot has no record of." />
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      {state !== 'ready' || !data ? <LoadStateView state={state} onRetry={() => void load()} /> : (
        <Stack spacing={4}>
          <Box component="section">
            <Typography variant="h6" component="h2">How each account's charges are read</Typography>
            <TableContainer component={Paper} variant="outlined" sx={{ mt: 1, borderRadius: 2 }}>
              <Table size="small" aria-label="How each account's charges are read">
                <TableHead>
                  <TableRow>
                    <TableCell>Account</TableCell>
                    <TableCell>Charges</TableCell>
                    <TableCell>Faxes last checked</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {data.accounts.length === 0 && (
                    <TableRow><TableCell colSpan={3}>No provider accounts are set up.</TableCell></TableRow>
                  )}
                  {data.accounts.map((source) => (
                    <TableRow key={source.account_key}>
                      <TableCell>{source.label}</TableCell>
                      <TableCell>{source.sentence}</TableCell>
                      <TableCell>{lastChecked(source)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
            {data.trunk.sources.length > 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                Source for the carrier trunk:{' '}
                {data.trunk.sources.map((source, index) => (
                  <span key={source.url}>
                    {index > 0 && ', '}
                    <Link href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</Link>
                  </span>
                ))}
              </Typography>
            )}
          </Box>

          {data.received.some((item) => item.summary) && (
            <Box component="section">
              <Typography variant="h6" component="h2">Received faxes</Typography>
              {data.received.filter((item) => item.summary).map((item) => (
                <Typography key={item.provider_id} variant="body2" sx={{ mt: 1 }}>{item.summary}</Typography>
              ))}
            </Box>
          )}

          <Box component="section">
            <Box display="flex" alignItems="center" justifyContent="space-between" gap={2} flexWrap="wrap">
              <Typography variant="h6" component="h2">Faxes Faxbot has no record of</Typography>
              {canWrite && listing && (
                <Button variant="outlined" onClick={() => void checkNow()} disabled={checking}>
                  {checking ? 'Checking…' : 'Check now'}
                </Button>
              )}
            </Box>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Faxes sent from a provider's own website, or ones Faxbot lost track of. Faxbot only lists them; it never
              sends or changes them.
            </Typography>
            {data.unrecorded.length === 0 ? (
              <Alert severity="success" sx={{ borderRadius: 2 }}>
                {listing ? 'Your providers listed no fax that Faxbot has no record of.'
                  : 'None of your accounts is at a provider that lists its faxes.'}
              </Alert>
            ) : (
              <TableContainer component={Paper} variant="outlined" sx={{ borderRadius: 2 }}>
                <Table size="small" aria-label="Faxes Faxbot has no record of">
                  <TableHead>
                    <TableRow>
                      <TableCell>What the provider billed</TableCell>
                      <TableCell>When</TableCell>
                      <TableCell align="right">Cost</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {data.unrecorded.map((fax) => (
                      <TableRow key={fax.id} data-testid="unrecorded-fax">
                        <TableCell>{fax.summary}</TableCell>
                        <TableCell>{formatServerTime(fax.time)}</TableCell>
                        <TableCell align="right">{fax.cost ? formatMoney(fax.cost) : 'No price'}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
          </Box>
        </Stack>
      )}
    </Box>
  );
}
