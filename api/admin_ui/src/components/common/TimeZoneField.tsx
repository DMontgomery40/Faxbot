// The installation's time zone, chosen from the IANA zones this browser knows, searchable by typing.
import { useMemo } from 'react';
import { Autocomplete, Box, Button, TextField } from '@mui/material';

export const TIME_ZONE_HELP = "Choose your office's time zone so the times in fax emails match your clocks.";

// Every IANA zone the browser knows, with UTC, sorted.
export function timeZones(): string[] {
  let zones: string[] = [];
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
    zones = supported ? supported('timeZone') : [];
  } catch {
    zones = [];
  }
  return [...new Set([...zones, 'UTC'])].sort();
}

// This computer's own zone, offered when the installation has none yet.
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch {
    return '';
  }
}

export default function TimeZoneField({ value, onChange, disabled }: {
  value: string;
  onChange: (zone: string) => void;
  disabled?: boolean;
}) {
  const options = useMemo(() => {
    const zones = timeZones();
    return value && !zones.includes(value) ? [value, ...zones] : zones;
  }, [value]);
  const here = browserTimeZone();
  return (
    <Box>
      <Autocomplete options={options} value={value || null} disabled={disabled} autoHighlight
        onChange={(_, next) => onChange(next ?? '')}
        renderInput={(params) => (
          <TextField {...params} size="small" label="Time zone" helperText={TIME_ZONE_HELP} />
        )} />
      {!value && here && options.includes(here) && (
        <Button size="small" sx={{ mt: 1 }} disabled={disabled} onClick={() => onChange(here)}>
          Use this computer&apos;s time zone ({here})
        </Button>
      )}
    </Box>
  );
}
