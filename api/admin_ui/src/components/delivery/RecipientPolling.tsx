import { useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, FormControlLabel, Stack, Switch, TextField, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { RecipientHold, RecipientPolling } from '../../api/types';
import { DeliveryError } from './shared';

// Recipients, Details: collecting faxes from another site's fax server by calling it (T.30 polling, M21), and
// holding faxes for that site to collect from Faxbot (polled transmission). Your side places the call in the
// first case; the other site places it in the second, so the cheaper side pays. Both are off unless you turn
// them on for the number. Faxbot collects only when a person selects Collect now, or at the times on the
// timetable that person set. A polling password is kept sealed: the screen says only whether one is set.

export default function RecipientPollingPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<RecipientPolling | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [label, setLabel] = useState('');
  const [selective, setSelective] = useState('');
  const [password, setPassword] = useState('');
  const [clearPassword, setClearPassword] = useState(false);
  const [times, setTimes] = useState('');
  const [days, setDays] = useState('');
  const [zone, setZone] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  const show = (loaded: RecipientPolling) => {
    setView(loaded);
    setEnabled(loaded.enabled);
    setLabel(loaded.label ?? '');
    setSelective(loaded.selective ?? '');
    setTimes(loaded.collect_times ?? '');
    setDays(loaded.collect_days ?? '');
    setZone(loaded.time_zone ?? '');
    setPassword('');
    setClearPassword(false);
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

  // null keeps the password as it is; '' clears it; digits set a new one.
  const passwordChange = clearPassword ? '' : (password ? password : null);
  const timetableChange = (value: string, before: string | null) => (value === (before ?? '') ? null : value);

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
        <TextField size="small" type="password" label="Polling password" value={password}
          disabled={!canWrite || busy || clearPassword} autoComplete="off"
          helperText={view.has_password ? 'A password is set; leave empty to keep it' : 'Only if the other fax server asks for one'}
          onChange={(event) => setPassword(event.target.value)} />
      </Stack>
      {view.has_password && canWrite && (
        <FormControlLabel sx={{ mt: 0.5 }}
          control={<Switch size="small" checked={clearPassword} disabled={busy} onChange={(event) => setClearPassword(event.target.checked)} />}
          label="Remove the password" />
      )}
      <Typography variant="body2" sx={{ mt: 1 }} data-testid="polling-timetable">
        {view.timetable ?? 'Faxbot collects only when you select Collect now.'}
      </Typography>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}>
        <TextField size="small" label="Collect at" value={times} disabled={!canWrite || busy} placeholder="08:00,16:00"
          helperText="Times of day, separated by commas" onChange={(event) => setTimes(event.target.value)} />
        <TextField size="small" label="On days" value={days} disabled={!canWrite || busy} placeholder="mon,tue,wed,thu,fri"
          helperText="mon to sun, separated by commas" onChange={(event) => setDays(event.target.value)} />
        <TextField size="small" label="Time zone" value={zone} disabled={!canWrite || busy} placeholder="America/Denver"
          helperText="The other site's time zone" onChange={(event) => setZone(event.target.value)} />
      </Stack>
      {message && <Alert severity="info" sx={{ mt: 1 }}>{message}</Alert>}
      {error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null}
      {canWrite && (
        <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
          <Button size="small" disabled={busy}
            onClick={() => run(() => client.savePolling(number, { enabled, label: label || null,
              selective: selective || null, password: passwordChange,
              collect_times: timetableChange(times, view.collect_times),
              collect_days: timetableChange(days, view.collect_days),
              time_zone: timetableChange(zone, view.time_zone) }), 'Saved.')}>
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
      <RecipientHoldSection client={client} number={number} canWrite={canWrite} />
    </Box>
  );
}

// Faxes this number collects from Faxbot: the other site calls, and Faxbot's fax engine sends what is held.
function RecipientHoldSection({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<RecipientHold | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [label, setLabel] = useState('');
  const [selective, setSelective] = useState('');
  const [password, setPassword] = useState('');
  const [clearPassword, setClearPassword] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const fileInput = useRef<HTMLInputElement | null>(null);

  const show = (loaded: RecipientHold) => {
    setView(loaded);
    setEnabled(loaded.enabled);
    setLabel(loaded.label ?? '');
    setSelective(loaded.selective ?? '');
    setPassword('');
    setClearPassword(false);
  };

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    setMessage('');
    client.getHold(number).then((loaded) => { if (live) show(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const run = async (step: () => Promise<RecipientHold & { sentence?: string }>, done: string) => {
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

  const passwordChange = clearPassword ? '' : (password ? password : null);

  return (
    <Box mt={2} data-testid="recipient-hold">
      <Typography variant="subtitle2">Faxes this number collects from Faxbot</Typography>
      <Typography variant="body2" color="text.secondary" data-testid="hold-note">{view.note}</Typography>
      <FormControlLabel
        control={<Switch checked={enabled} disabled={!canWrite || busy} onChange={(event) => setEnabled(event.target.checked)} />}
        label="Let this number call Faxbot and collect the faxes held for it" />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}>
        <TextField size="small" label="Name of the other site" value={label} disabled={!canWrite || busy}
          placeholder="Denver office" onChange={(event) => setLabel(event.target.value)} />
        <TextField size="small" label="Selective polling address it must give" value={selective}
          disabled={!canWrite || busy} helperText="Leave empty for none"
          onChange={(event) => setSelective(event.target.value)} />
        <TextField size="small" type="password" label="Polling password it must give" value={password}
          disabled={!canWrite || busy || clearPassword} autoComplete="off"
          helperText={view.has_password ? 'A password is set; leave empty to keep it' : 'Leave empty for none'}
          onChange={(event) => setPassword(event.target.value)} />
      </Stack>
      {view.has_password && canWrite && (
        <FormControlLabel sx={{ mt: 0.5 }}
          control={<Switch size="small" checked={clearPassword} disabled={busy} onChange={(event) => setClearPassword(event.target.checked)} />}
          label="Remove the password" />
      )}
      {message && <Alert severity="info" sx={{ mt: 1 }}>{message}</Alert>}
      {error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null}
      {canWrite && (
        <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
          <Button size="small" disabled={busy}
            onClick={() => run(() => client.saveHold(number, { enabled, label: label || null,
              selective: selective || null, password: passwordChange }), 'Saved.')}>
            Save holding
          </Button>
          <Button size="small" variant="outlined" disabled={busy || !view.enabled} onClick={() => fileInput.current?.click()}>
            Hold a fax for it
          </Button>
          <input ref={fileInput} type="file" hidden data-testid="hold-file" accept="application/pdf,image/tiff"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              if (file) void run(() => client.holdFax(number, file), 'Held.');
            }} />
        </Stack>
      )}
      {view.held.length > 0 && (
        <Box component="ul" sx={{ my: 1, pl: 3 }} data-testid="held-faxes">
          {view.held.map((item) => (
            <li key={item.id}>
              <Typography variant="body2" component="span">
                {`${item.held}: ${item.name ?? 'Document'}, ${item.pages} page${item.pages === 1 ? '' : 's'}. ${item.state}. ${item.sentence}`}
              </Typography>
              {canWrite && !item.gone && (
                <Button size="small" sx={{ ml: 1 }} disabled={busy}
                  onClick={() => run(() => client.withdrawHeldFax(number, item.id), 'Withdrawn.')}>
                  Withdraw
                </Button>
              )}
            </li>
          ))}
        </Box>
      )}
    </Box>
  );
}
