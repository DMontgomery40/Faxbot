import { useEffect, useState } from 'react';
import { Alert, Box, Button, FormControlLabel, MenuItem, Stack, Switch, TextField, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { RecipientCodingTuning } from '../../api/sipTypes';
import { DeliveryError } from './shared';

// Recipients, Details: smaller pages (lossless tuning) for one number. The server words every sentence: what this
// number's calls use and why, and the warning beside the switch for the smallest page format.

export default function RecipientCodingTuningPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<RecipientCodingTuning | null>(null);
  const [smaller, setSmaller] = useState('');
  const [smallest, setSmallest] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  const show = (loaded: RecipientCodingTuning) => {
    setView(loaded);
    setSmaller(loaded.tune === false ? 'off' : '');
    setSmallest(loaded.tune_jbig);
  };

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    setSaved(false);
    client.getCodingTuning(number).then((loaded) => { if (live) show(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const changed = smaller !== (view.tune === false ? 'off' : '') || smallest !== view.tune_jbig;

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      show(await client.saveCodingTuning(number, { tune: smaller === 'off' ? false : null, tune_jbig: smallest }));
      setSaved(true);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box mt={2} data-testid="recipient-coding-tuning">
      <Typography variant="subtitle2">Smaller pages</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }} data-testid="coding-tuning-now">
        {view.jbig_sentence}
      </Typography>
      {view.reasons.map((reason) => (
        <Typography key={reason} variant="body2" color="text.secondary">{reason}</Typography>
      ))}
      <DeliveryError error={error} onClose={() => setError(null)} />
      {saved && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setSaved(false)}>Saved for the next fax.</Alert>}
      <Stack spacing={1} sx={{ mt: 1 }}>
        <TextField select size="small" label="Smaller pages for this number" value={smaller} sx={{ maxWidth: 320 }}
          disabled={!canWrite || busy} onChange={(event) => setSmaller(event.target.value)}>
          <MenuItem value="">As set for all faxes</MenuItem>
          <MenuItem value="off">Off</MenuItem>
        </TextField>
        <div>
          <FormControlLabel label="Also use the smallest page format for this number"
            control={<Switch checked={smallest} disabled={!canWrite || busy || smaller === 'off'}
              onChange={(event) => setSmallest(event.target.checked)} />} />
          <Typography variant="body2" color="text.secondary" data-testid="coding-tuning-warning">
            {view.warning}
          </Typography>
        </div>
      </Stack>
      {canWrite && (
        <Button size="small" sx={{ mt: 1 }} onClick={save} disabled={busy || !changed}>Save for this number</Button>
      )}
    </Box>
  );
}
