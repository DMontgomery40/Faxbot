import { useEffect, useId, useState } from 'react';
import {
  Alert,
  FormControl,
  FormControlLabel,
  FormHelperText,
  InputLabel,
  Link,
  MenuItem,
  Select,
  Stack,
  Switch,
  TextField,
  Typography,
} from '@mui/material';
import type AdminAPIClient from '../api/client';
import type { EfaxStatus, Settings } from '../api/types';
import { parseServerTime } from '../api/time';
import SecretInput from './common/SecretInput';
import EnvSetField, { environmentManaged } from './common/EnvSetField';
import { numberHint, numberPlaceholder, settingsNumberFormat } from './common/numbers';

// The eFax section of the Setup Wizard and Settings: the eFax Enterprise API
// account (sold as eFax Corporate) and how Faxbot receives through it. Both
// places keep these values in their own form and save them with their other
// settings; this component only shows and edits them.

type FormValue = string | number | boolean;

export const EFAX_FIELDS = ['efax_app_id', 'efax_api_key', 'efax_user_id', 'efax_caller_id', 'efax_csid',
  'efax_poll_seconds', 'efax_delete_after_download', 'efax_webhook_secret'];

// The loaded eFax values under their settings names; saved keys arrive hidden.
export function efaxEditorValues(data: Settings): Record<string, FormValue> {
  return {
    efax_app_id: data.efax?.app_id ?? '',
    efax_api_key: data.efax?.api_key ?? '',
    efax_user_id: data.efax?.user_id ?? '',
    efax_caller_id: data.efax?.caller_id ?? '',
    efax_csid: data.efax?.csid ?? '',
    efax_poll_seconds: data.efax?.poll_seconds ?? 60,
    efax_delete_after_download: data.efax?.delete_after_download ?? false,
    efax_webhook_secret: data.efax?.webhook_secret ?? '',
  };
}

const CREDENTIALS = [
  { key: 'efax_app_id', label: 'App ID' },
  { key: 'efax_api_key', label: 'API key' },
  { key: 'efax_user_id', label: 'User ID' },
];

export const INTERVALS = [
  { seconds: 30, text: 'Every 30 seconds' },
  { seconds: 60, text: 'Every minute' },
  { seconds: 120, text: 'Every 2 minutes' },
  { seconds: 300, text: 'Every 5 minutes' },
  { seconds: 900, text: 'Every 15 minutes' },
  { seconds: 1800, text: 'Every 30 minutes' },
  { seconds: 3600, text: 'Every hour' },
];

export function intervalText(seconds: number): string {
  const known = INTERVALS.find((item) => item.seconds === seconds);
  if (known) return known.text;
  return seconds % 60 === 0 ? `Every ${seconds / 60} minutes` : `Every ${seconds} seconds`;
}

interface EfaxSettingsProps {
  values: Record<string, FormValue | undefined>;
  onChange: (field: string, value: FormValue) => void;
  settings: Settings | null;
  disabled?: boolean;
  // eFax is the receiving provider: show how Faxbot collects received faxes.
  receives?: boolean;
  docsHref?: string;
  // Reads whether Faxbot is checking eFax and which received faxes are still stored there.
  client?: AdminAPIClient;
}

// What Faxbot last saw at eFax, for the receiving part of the section.
function useEfaxStatus(client: AdminAPIClient | undefined, active: boolean): EfaxStatus | null {
  const [status, setStatus] = useState<EfaxStatus | null>(null);
  useEffect(() => {
    if (!client || !active) {
      setStatus(null);
      return undefined;
    }
    let current = true;
    client.getEfaxStatus().then((value) => { if (current) setStatus(value); })
      .catch(() => { if (current) setStatus(null); });
    return () => { current = false; };
  }, [client, active]);
  return status;
}

function checkedText(value: string | null): string | null {
  const when = parseServerTime(value);
  return when ? `Faxbot last checked eFax at ${when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}.` : null;
}

export default function EfaxSettings({ values, onChange, settings, disabled, receives = false, docsHref,
  client }: EfaxSettingsProps) {
  const id = useId();
  const status = useEfaxStatus(client, receives);
  const checked = status?.receiving ? checkedText(status.checked_at) : null;
  const managed = environmentManaged(settings);
  const format = settingsNumberFormat(settings);
  const seconds = Number(values.efax_poll_seconds ?? 60) || 60;
  const options = INTERVALS.some((item) => item.seconds === seconds) ? INTERVALS
    : [...INTERVALS, { seconds, text: intervalText(seconds) }].sort((a, b) => a.seconds - b.seconds);
  return (
    <Stack spacing={2} sx={{ mt: 1 }} data-testid="efax-settings">
      <Typography variant="body2">
        eFax sends the app ID, API key and user ID in its welcome email once your eFax Corporate account has API access.
        {docsHref && <> <Link href={docsHref} target="_blank" rel="noreferrer">How to get them</Link></>}
      </Typography>
      {CREDENTIALS.map(({ key, label }) => managed.has(key)
        ? <EnvSetField key={key} fullWidth label={label} />
        : <SecretInput key={key} fullWidth disabled={disabled} label={label} value={String(values[key] ?? '')}
            onChange={(value) => onChange(key, value)} />)}
      <TextField fullWidth disabled={disabled} label="Caller ID (optional)" value={String(values.efax_caller_id ?? '')}
        placeholder={numberPlaceholder(format)} onChange={(event) => onChange('efax_caller_id', event.target.value)}
        helperText={`${numberHint(format, 'The eFax number recipients see')} Leave empty for your account’s number.`} />
      <TextField fullWidth disabled={disabled} label="Station name (optional)" value={String(values.efax_csid ?? '')}
        inputProps={{ maxLength: 20 }} onChange={(event) => onChange('efax_csid', event.target.value)}
        helperText="Up to 20 characters the receiving fax machine shows as the sender." />
      {receives && <>
        <FormControl fullWidth disabled={disabled}>
          <InputLabel id={`${id}-poll`}>Check eFax for received faxes</InputLabel>
          <Select labelId={`${id}-poll`} label="Check eFax for received faxes" value={seconds}
            onChange={(event) => onChange('efax_poll_seconds', Number(event.target.value))}>
            {options.map((item) => <MenuItem key={item.seconds} value={item.seconds}>{item.text}</MenuItem>)}
          </Select>
          <FormHelperText>Faxbot asks eFax for new faxes, so nothing needs to reach Faxbot from the internet.</FormHelperText>
        </FormControl>
        <FormControlLabel disabled={disabled} label="Delete each fax from eFax once Faxbot has stored it"
          control={<Switch checked={!!values.efax_delete_after_download}
            onChange={(event) => onChange('efax_delete_after_download', event.target.checked)} />} />
        <Typography variant="body2" color="text.secondary">
          {values.efax_delete_after_download ? 'eFax keeps no copy after Faxbot stores a fax.'
            : 'A copy stays in your eFax account.'}
        </Typography>
        {managed.has('efax_webhook_secret')
          ? <EnvSetField fullWidth label="Notification secret (optional)" />
          : <SecretInput fullWidth disabled={disabled} label="Notification secret (optional)"
              value={String(values.efax_webhook_secret ?? '')} onChange={(value) => onChange('efax_webhook_secret', value)}
              helperText="With a secret, eFax can tell Faxbot the moment a fax arrives; the eFax guide says what to give eFax." />}
        {checked && !status?.problem && <Typography variant="body2" data-testid="efax-checked">{checked}</Typography>}
        {status?.receiving && status.problem && <Alert severity="warning">{status.problem}</Alert>}
        {status?.notes.map((note) => <Alert key={note} severity="info" data-testid="efax-note">{note}</Alert>)}
      </>}
    </Stack>
  );
}
