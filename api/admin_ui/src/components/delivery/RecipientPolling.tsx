import { useEffect, useState } from 'react';
import { Alert, Box, Button, FormControlLabel, Stack, Switch, TextField, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { RecipientPolling } from '../../api/types';
import { DeliveryError } from './shared';

// Recipients, Details: collecting faxes from another site's fax server by calling it (T.30 polling, M21).
// Your side places the call and the other server sends the fax it holds for you. Off unless you turn it on for
// the number, and Faxbot never collects by itself: a person selects Collect now each time.

export default function RecipientPollingPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<RecipientPolling | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [label, setLabel] = useState('');
  const [selective, setSelective] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  const show = (loaded: RecipientPolling) => {
    setView(loaded);
    setEnabled(loaded.enabled);
    setLabel(loaded.label ?? '');
    setSelective(loaded.selective ?? '');
  };

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    setMessage('');
    client.getPolling(number).then((loaded) => { if (live) show(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const run = async (step: () => Promise<RecipientPolling & { sentence?: string }>, done: string) => {
    setBusy(true);
    setError(null);
    setMessage('');
    try {
      const answer = await step();
      show(answer);
      setMessage(answer.sentence ?? done);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box mt={2} data-testid="recipient-polling">
      <Typography variant="subtitle2">Collect faxes from this number</Typography>
      {view.advice && <Typography variant="body2" data-testid="polling-advice">{view.advice}</Typography>}
      <Typography variant="body2" color="text.secondary" data-testid="polling-note">{view.note}</Typography>
      <FormControlLabel
        control={<Switch checked={enabled} disabled={!canWrite || busy} onChange={(event) => setEnabled(event.target.checked)} />}
        label="Allow Faxbot to collect faxes this number holds for you" />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}>
        <TextField size="small" label="Other site" value={label} disabled={!canWrite || busy}
          placeholder="Denver office" onChange={(event) => setLabel(event.target.value)} />
        <TextField size="small" label="Selective polling address" value={selective} disabled={!canWrite || busy}
          helperText="Only if the other fax server asks for one"
          onChange={(event) => setSelective(event.target.value)} />
      </Stack>
      {message && <Alert severity="info" sx={{ mt: 1 }}>{message}</Alert>}
      {error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null}
      {canWrite && (
        <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
          <Button size="small" disabled={busy}
            onClick={() => run(() => client.savePolling(number, { enabled, label: label || null,
              selective: selective || null }), 'Saved.')}>
            Save collecting
          </Button>
          <Button size="small" variant="outlined" disabled={busy || !view.enabled}
            onClick={() => run(() => client.collectPolling(number), 'Collecting.')}>
            Collect now
          </Button>
        </Stack>
      )}
      {view.requests.length > 0 && (
        <Box component="ul" sx={{ my: 1, pl: 3 }} data-testid="polling-requests">
          {view.requests.map((item) => (
            <li key={item.id}>
              <Typography variant="body2">{`${item.requested}: ${item.state}. ${item.sentence}`}</Typography>
            </li>
          ))}
        </Box>
      )}
    </Box>
  );
}
