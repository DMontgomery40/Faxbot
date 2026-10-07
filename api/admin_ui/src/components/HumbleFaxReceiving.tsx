import { useCallback, useEffect, useId, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  FormControl,
  FormControlLabel,
  FormHelperText,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  Typography,
} from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../api/client';
import type { HumbleFaxStatus, Settings } from '../api/types';
import { parseServerTime } from '../api/time';
import { INTERVALS, intervalText } from './EfaxSettings';

// Receiving through HumbleFax, inside the HumbleFax section of Settings: the
// switch, how often Faxbot asks HumbleFax, when it last checked and Check now.
// Settings keeps these values in its own form and saves them with the others.

type FormValue = string | number | boolean;

export const HUMBLEFAX_RECEIVING_FIELDS = ['humblefax_receive_enabled', 'humblefax_poll_seconds'];

export function humblefaxReceivingValues(data: Settings): Record<string, FormValue> {
  return {
    humblefax_receive_enabled: data.humblefax?.receive ?? false,
    humblefax_poll_seconds: data.humblefax?.poll_seconds ?? 60,
  };
}

interface HumbleFaxReceivingProps {
  values: Record<string, FormValue | undefined>;
  onChange: (field: string, value: FormValue) => void;
  disabled?: boolean;
  // HumbleFax is the receiving provider: it receives without the separate switch.
  receivingProvider?: boolean;
  client?: AdminAPIClient;
  // The saved settings; the status is read again after each save.
  settings?: Settings | null;
}

function plural(count: number): string {
  if (count === 0) return 'no new faxes';
  return count === 1 ? '1 new fax' : `${count} new faxes`;
}

// One sentence: what Faxbot last checked at HumbleFax, and when.
export function humblefaxStatusText(status: HumbleFaxStatus): string {
  if (!status.receiving) return status.reason ?? 'Faxbot is not checking HumbleFax.';
  const when = parseServerTime(status.checked_at);
  if (!when) return 'Faxbot has not checked HumbleFax yet.';
  const time = when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return `Faxbot last checked HumbleFax at ${time} and found ${plural(status.found ?? 0)}.`;
}

export default function HumbleFaxReceiving({ values, onChange, disabled, receivingProvider = false,
  client, settings }: HumbleFaxReceivingProps) {
  const id = useId();
  const [status, setStatus] = useState<HumbleFaxStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const switchedOn = !!values.humblefax_receive_enabled;
  const receives = receivingProvider || switchedOn;
  const seconds = Number(values.humblefax_poll_seconds ?? 60) || 60;
  const options = INTERVALS.some((item) => item.seconds === seconds) ? INTERVALS
    : [...INTERVALS, { seconds, text: intervalText(seconds) }].sort((a, b) => a.seconds - b.seconds);

  const load = useCallback(() => {
    if (!client) return undefined;
    let current = true;
    client.getHumbleFaxStatus().then((value) => { if (current) setStatus(value); })
      .catch(() => { if (current) setStatus(null); });
    return () => { current = false; };
  }, [client]);
  useEffect(() => load(), [load, settings]);

  const checkNow = async () => {
    if (!client) return;
    setChecking(true);
    setNotice(null);
    try {
      setStatus(await client.checkHumbleFaxNow());
    } catch (error) {
      const detail = error instanceof AdminAPIError ? error.detail : null;
      setNotice(error instanceof AdminAPIError && error.status === 403
        ? 'You do not have permission to do this.'
        : error instanceof AdminAPIError && [409, 429, 503].includes(error.status) && detail ? detail
          : 'Faxbot could not check HumbleFax right now. Try again.');
    } finally {
      setChecking(false);
    }
  };

  return (
    <Stack spacing={2} sx={{ mt: 2 }} data-testid="humblefax-receiving">
      {receivingProvider ? (
        <Typography variant="body2">
          HumbleFax is your receiving provider, so Faxbot collects the faxes it receives.
        </Typography>
      ) : (
        <Box>
          <FormControlLabel disabled={disabled} label="Receive faxes from HumbleFax"
            control={<Switch checked={switchedOn}
              onChange={(event) => onChange('humblefax_receive_enabled', event.target.checked)} />} />
          <Typography variant="body2" color="text.secondary">
            {switchedOn
              ? 'Faxbot collects the faxes your HumbleFax numbers receive, alongside any other way you receive.'
              : 'Faxes sent to your HumbleFax numbers stay only in your HumbleFax account.'}
          </Typography>
        </Box>
      )}
      {receives && (
        <FormControl fullWidth disabled={disabled}>
          <InputLabel id={`${id}-poll`}>Check HumbleFax for received faxes</InputLabel>
          <Select labelId={`${id}-poll`} label="Check HumbleFax for received faxes" value={seconds}
            onChange={(event) => onChange('humblefax_poll_seconds', Number(event.target.value))}>
            {options.map((item) => <MenuItem key={item.seconds} value={item.seconds}>{item.text}</MenuItem>)}
          </Select>
          <FormHelperText>
            Faxbot asks HumbleFax for new faxes, so nothing needs to reach Faxbot from the internet. The first check
            also brings in the faxes HumbleFax received in the last 30 days, and every fax stays in HumbleFax too.
          </FormHelperText>
        </FormControl>
      )}
      {/* Only what was saved is described; an unsaved switch says nothing yet. */}
      {status?.turned_on && (
        <Box>
          {status.receiving && status.problem
            ? <Alert severity="warning" data-testid="humblefax-problem">{status.problem}</Alert>
            : <Typography variant="body2" data-testid="humblefax-checked">{humblefaxStatusText(status)}</Typography>}
          {status.receiving && (
            <Button variant="outlined" size="small" sx={{ mt: 1 }} disabled={checking || !client}
              onClick={() => { void checkNow(); }}>
              Check now
            </Button>
          )}
          {notice && <Alert severity="error" sx={{ mt: 1 }} data-testid="humblefax-check-notice">{notice}</Alert>}
        </Box>
      )}
    </Stack>
  );
}
