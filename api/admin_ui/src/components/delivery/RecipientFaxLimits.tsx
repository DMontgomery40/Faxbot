import { useEffect, useState } from 'react';
import { Alert, Box, Button, MenuItem, Stack, TextField, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { RecipientFaxLimits } from '../../api/sipTypes';
import { DeliveryError } from './shared';

// Recipients, Details: whether this fax machine takes SSL Fax (learned from calls) and its own
// limits for a machine that keeps failing. Both fax engines use the limits on every call to it.

const SPEEDS = [14400, 9600, 7200, 4800];

export default function RecipientFaxLimitsPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<RecipientFaxLimits | null>(null);
  const [speed, setSpeed] = useState('');
  const [ecm, setEcm] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  const show = (loaded: RecipientFaxLimits) => {
    setView(loaded);
    setSpeed(loaded.max_rate ? String(loaded.max_rate) : '');
    setEcm(loaded.ecm === null ? '' : loaded.ecm ? 'on' : 'off');
  };

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    setSaved(false);
    client.getFaxLimits(number).then((loaded) => { if (live) show(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const changed = speed !== (view.max_rate ? String(view.max_rate) : '')
    || ecm !== (view.ecm === null ? '' : view.ecm ? 'on' : 'off');

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      show(await client.saveFaxLimits(number, { max_rate: speed ? Number(speed) : null,
        ecm: ecm === '' ? null : ecm === 'on' }));
      setSaved(true);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box mt={2} data-testid="recipient-fax-limits">
      <Typography variant="subtitle2">This fax machine</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }}>
        {view.sslfax_sentence
          ? `${view.sslfax_sentence}${view.accepts_sslfax_at ? ` Seen on ${new Date(view.accepts_sslfax_at).toLocaleDateString()}.` : ''}`
          : 'Faxbot finds out after the next call whether this fax machine can take pages faster.'}
      </Typography>
      <Typography variant="body2" color="text.secondary">
        If faxes to this number keep failing, a lower speed or error correction off can help.
      </Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {saved && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setSaved(false)}>Saved for the next fax.</Alert>}
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ mt: 1 }}>
        <TextField select size="small" label="Highest speed" value={speed} sx={{ minWidth: 220 }}
          disabled={!canWrite || busy} onChange={(event) => setSpeed(event.target.value)}>
          <MenuItem value="">As set for all faxes</MenuItem>
          {SPEEDS.map((rate) => <MenuItem key={rate} value={String(rate)}>{`${rate.toLocaleString()} bits per second`}</MenuItem>)}
        </TextField>
        <TextField select size="small" label="Error correction" value={ecm} sx={{ minWidth: 220 }}
          disabled={!canWrite || busy} onChange={(event) => setEcm(event.target.value)}>
          <MenuItem value="">As set for all faxes</MenuItem>
          <MenuItem value="on">On</MenuItem>
          <MenuItem value="off">Off</MenuItem>
        </TextField>
      </Stack>
      {canWrite && (
        <Button size="small" sx={{ mt: 1 }} onClick={save} disabled={busy || !changed}>Save for this number</Button>
      )}
    </Box>
  );
}
