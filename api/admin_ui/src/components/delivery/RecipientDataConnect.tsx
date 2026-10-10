// NTT Data Connect (routing/data_connect_store.py): whether this number's fax machine takes Data Connect calls, the
// NTT Hikari Denwa trunks that place them, and NTT's own limits, shown where you turn it on (Recipients → Details).
import { useEffect, useState } from 'react';
import { Alert, Box, FormControlLabel, Switch, Typography } from '@mui/material';
import type AdminAPIClient from '../../api/client';
import { AdminAPIError } from '../../api/client';
import { formatLocalDate } from '../../api/time';

type Client = Pick<AdminAPIClient, 'call'>;

interface Trunk { account: string; label: string; enabled: boolean; reason: string | null }

export interface DataConnectView {
  number: string;
  capable: boolean;
  known: boolean;
  source: 'admin' | 'learned' | null;
  learned_on: string | null;
  sentence: string;
  trunks: Trunk[];
  advice: string[];
  price: string;
  not_run: string;
}

const path = (value: string) => encodeURIComponent(value);

function problem(error: unknown): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return 'This could not be saved. Try again.';
}

export function RecipientDataConnect({ client, number, canWrite }: { client: Client; number: string; canWrite: boolean }) {
  const [view, setView] = useState<DataConnectView | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.call<DataConnectView>({ method: 'GET', path: `/routing/destinations/${path(number)}/data-connect` })
      .then((found) => { if (live) { setView(found); setFailed(false); } })
      .catch(() => { if (live) { setView(null); setFailed(true); } });
    return () => { live = false; };
  }, [client, number]);
  if (failed) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
        Whether this number takes NTT Data Connect calls could not be loaded. Close and open this number to try again.
      </Typography>
    );
  }
  // Only for an installation with an NTT Hikari Denwa trunk, or a number Faxbot already knows about.
  if (!view || (view.trunks.length === 0 && !view.known)) return null;
  const setCapable = async (capable: boolean) => {
    setBusy(true);
    setMessage(null);
    try {
      setView(await client.call<DataConnectView>({ method: 'PUT', path: `/routing/destinations/${path(number)}/data-connect`,
        body: { capable } }));
    } catch (failure) {
      setMessage(problem(failure));
    } finally {
      setBusy(false);
    }
  };
  const setTrunk = async (account: string, enabled: boolean) => {
    setBusy(true);
    setMessage(null);
    try {
      const saved = await client.call<Trunk>({ method: 'PUT', path: `/routing/data-connect/trunks/${path(account)}`,
        body: { enabled } });
      setView({ ...view, trunks: view.trunks.map((item) => (item.account === account ? { ...item, ...saved } : item)) });
    } catch (failure) {
      setMessage(problem(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Box sx={{ mt: 2 }} role="region" aria-label="NTT Data Connect">
      <Typography variant="subtitle2">NTT Data Connect</Typography>
      <Typography variant="body2" color="text.secondary" data-testid="data-connect-sentence">
        {view.sentence}{view.learned_on ? ` (${formatLocalDate(view.learned_on)})` : ''}
      </Typography>
      <FormControlLabel
        control={<Switch checked={view.capable} disabled={!canWrite || busy}
          onChange={(event) => void setCapable(event.target.checked)} />}
        label="This number's fax machine takes Data Connect calls" />
      {view.trunks.map((trunk) => (
        <Box key={trunk.account}>
          <FormControlLabel
            control={<Switch checked={trunk.enabled} disabled={!canWrite || busy}
              onChange={(event) => void setTrunk(trunk.account, event.target.checked)} />}
            label={`Data Connect calls over ${trunk.label}`} />
          {trunk.reason && <Typography variant="body2" color="text.secondary">{trunk.reason}</Typography>}
        </Box>
      ))}
      <Typography variant="body2" sx={{ mt: 1 }} data-testid="data-connect-price">{view.price}</Typography>
      <Box component="ul" sx={{ mt: 1, mb: 0, pl: 3 }} data-testid="data-connect-advice">
        {view.advice.map((item) => <Typography component="li" variant="body2" color="text.secondary" key={item}>{item}</Typography>)}
      </Box>
      <Typography variant="body2" color="text.secondary">{view.not_run}</Typography>
      {message && <Alert severity="error" sx={{ mt: 1 }} onClose={() => setMessage(null)}>{message}</Alert>}
    </Box>
  );
}
