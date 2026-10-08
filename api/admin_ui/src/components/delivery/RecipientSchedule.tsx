import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, FormControlLabel, FormGroup, MenuItem, Stack, Switch, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { RecipientSchedule } from '../../api/types';
import { DeliveryError } from './shared';

// Recipients, Details: when Faxbot sends to this recipient. The hours it takes faxes (its own request, such as
// business hours only, in its time zone) and the hours its line is usually busy, learned from earlier calls.

const DAYS: [string, string][] = [
  ['mon', 'Mon'], ['tue', 'Tue'], ['wed', 'Wed'], ['thu', 'Thu'], ['fri', 'Fri'], ['sat', 'Sat'], ['sun', 'Sun'],
];
const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri'];
const COMMON_ZONES = [
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Phoenix', 'America/Los_Angeles',
  'America/Anchorage', 'Pacific/Honolulu', 'America/Toronto', 'America/Vancouver', 'Europe/London',
  'Europe/Dublin', 'Australia/Sydney', 'Australia/Melbourne', 'Australia/Brisbane', 'Australia/Perth',
  'Pacific/Auckland',
];

function zones(): string[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
  try {
    const all = supported ? supported('timeZone') : [];
    return all.length ? all : COMMON_ZONES;
  } catch {
    return COMMON_ZONES;
  }
}

export default function RecipientSchedulePanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<RecipientSchedule | null>(null);
  const [anyTime, setAnyTime] = useState(true);
  const [days, setDays] = useState<string[]>(WEEKDAYS);
  const [start, setStart] = useState('09:00');
  const [end, setEnd] = useState('17:00');
  const [zone, setZone] = useState('');
  const [learn, setLearn] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  const show = (loaded: RecipientSchedule) => {
    setView(loaded);
    const always = loaded.days === null && loaded.start === null;
    setAnyTime(always);
    setDays(loaded.days ?? (always ? WEEKDAYS : DAYS.map(([day]) => day)));
    setStart(loaded.start ?? '09:00');
    setEnd(loaded.end ?? '17:00');
    setZone(loaded.time_zone);
    setLearn(loaded.learn_busy);
  };

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    setSaved(false);
    client.getSchedule(number).then((loaded) => { if (live) show(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const toggleDay = (day: string) => setDays((current) => (
    current.includes(day) ? current.filter((item) => item !== day) : [...current, day]));

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const ordered = DAYS.map(([day]) => day).filter((day) => days.includes(day));
      show(await client.saveSchedule(number, {
        time_zone: zone || null,
        days: anyTime ? null : ordered,
        start: anyTime ? null : start,
        end: anyTime ? null : end,
        learn_busy: learn,
      }));
      setSaved(true);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const installationZone = view.installation_time_zone || 'UTC';

  return (
    <Box mt={2} data-testid="recipient-schedule">
      <Typography variant="subtitle2">When to send</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }} data-testid="schedule-hours">{view.hours_sentence}</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {saved && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setSaved(false)}>Saved for the next fax.</Alert>}
      <FormControlLabel sx={{ mt: 1 }}
        control={<Checkbox checked={anyTime} disabled={!canWrite || busy}
          onChange={(event) => setAnyTime(event.target.checked)} inputProps={{ 'aria-label': 'Takes faxes at any time' }} />}
        label="Takes faxes at any time" />
      {!anyTime && (
        <Box data-testid="schedule-days">
          <FormGroup row>
            {DAYS.map(([day, label]) => (
              <FormControlLabel key={day} label={label}
                control={<Checkbox size="small" checked={days.includes(day)} disabled={!canWrite || busy}
                  onChange={() => toggleDay(day)} />} />
            ))}
          </FormGroup>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ mt: 1 }}>
            <TextField type="time" size="small" label="From" value={start} disabled={!canWrite || busy}
              onChange={(event) => setStart(event.target.value)} InputLabelProps={{ shrink: true }} />
            <TextField type="time" size="small" label="Until" value={end} disabled={!canWrite || busy}
              onChange={(event) => setEnd(event.target.value)} InputLabelProps={{ shrink: true }} />
          </Stack>
        </Box>
      )}
      <TextField select size="small" label="Recipient's time zone" value={zone} sx={{ mt: 2, minWidth: 260 }}
        disabled={!canWrite || busy} onChange={(event) => setZone(event.target.value)}>
        <MenuItem value="">{`The same as yours (${installationZone})`}</MenuItem>
        {zones().map((name) => <MenuItem key={name} value={name}>{name}</MenuItem>)}
      </TextField>

      <Typography variant="subtitle2" sx={{ mt: 2 }}>Busy hours</Typography>
      <FormControlLabel
        control={<Switch checked={learn} disabled={!canWrite || busy} onChange={(event) => setLearn(event.target.checked)} />}
        label="Learn the hours this number is usually busy, slow or failing" />
      {view.busy_hours.length > 0 && (
        <Box component="ul" sx={{ my: 0.5, pl: 3 }} data-testid="schedule-busy-hours">
          {view.busy_hours.map((item) => (
            <li key={item.label}><Typography variant="body2">{`${item.label}: ${item.sentence}`}</Typography></li>
          ))}
        </Box>
      )}
      <Typography variant="body2" color="text.secondary" data-testid="schedule-busy-sentence">{view.busy_sentence}</Typography>
      {view.call_hours_sentence && (
        <>
          <Typography variant="subtitle2" sx={{ mt: 2 }}>Call hours</Typography>
          {(view.call_hours ?? []).length > 0 && (
            <Box component="ul" sx={{ my: 0.5, pl: 3 }} data-testid="schedule-call-hours">
              {(view.call_hours ?? []).map((item) => (
                <li key={item.label}><Typography variant="body2">{`${item.label}: ${item.sentence}`}</Typography></li>
              ))}
            </Box>
          )}
          {view.typical_hour && (
            <Typography variant="body2" data-testid="schedule-typical-hour">{`Any hour: ${view.typical_hour}`}</Typography>
          )}
          <Typography variant="body2" color="text.secondary" data-testid="schedule-call-hours-sentence">
            {view.call_hours_sentence}
          </Typography>
        </>
      )}
      <Typography variant="caption" color="text.secondary" display="block" data-testid="schedule-failed-try">
        {view.failed_try.sentence}
      </Typography>
      {canWrite && (
        <Button size="small" sx={{ mt: 1 }} onClick={save} disabled={busy || (!anyTime && days.length === 0)}>
          Save when to send
        </Button>
      )}
    </Box>
  );
}
