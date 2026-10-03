// Fax numbers follow the installation country. The server stores every number
// in international form and reads a number typed without a country code the
// way people in that country dial it, so the console only explains the format
// and never checks digits itself.
import { useEffect, useMemo, useState } from 'react';
import { Autocomplete, TextField, createFilterOptions } from '@mui/material';
import type AdminAPIClient from '../../api/client';
import type { ConsoleContext, NumberFormat, Settings } from '../../api/types';

let regionNames: Intl.DisplayNames | null | undefined;

// The English name of a country code, or the code when no name is known.
export function countryName(code: string): string {
  if (regionNames === undefined) {
    try {
      regionNames = new Intl.DisplayNames(['en'], { type: 'region' });
    } catch {
      regionNames = null;
    }
  }
  try {
    return regionNames?.of(code) || code;
  } catch {
    return code;
  }
}

export function countryOptions(codes: readonly string[]): Array<{ value: string; label: string }> {
  return codes
    .map((code) => ({ value: code, label: countryName(code) }))
    .sort((a, b) => a.label.localeCompare(b.label, 'en'));
}

export function settingsNumberFormat(settings: Settings | null | undefined): NumberFormat | null {
  const numbers = settings?.numbers;
  if (!numbers?.default_country) return null;
  return {
    country: numbers.default_country,
    national: numbers.example?.national ?? '',
    international: numbers.example?.international ?? '',
  };
}

export function contextNumberFormat(send: ConsoleContext['send'] | undefined): NumberFormat | null {
  if (!send?.default_country) return null;
  return { country: send.default_country, national: send.number_example ?? '', international: '' };
}

// A sample to show inside an empty number field.
export function numberPlaceholder(format: NumberFormat | null | undefined): string | undefined {
  return format?.national.trim() || format?.international.trim() || undefined;
}

// One sentence on how to type a number, led by what the field is for when given.
export function numberHint(format: NumberFormat | null | undefined, lead?: string): string {
  const national = format?.national.trim() ?? '';
  const international = format?.international.trim() ?? '';
  const example = national && international ? `${national} or ${international}`
    : national ? `${national}, or a number starting with + and its country code`
    : international;
  if (example) return lead ? `${lead}, for example ${example}.` : `For example ${example}.`;
  return lead
    ? `${lead}, as you dial it or starting with + and its country code.`
    : 'Type the number as you dial it, or start with + and its country code.';
}

// For numbers the server keeps exactly as entered: international form only.
export function internationalHint(format: NumberFormat | null | undefined, lead: string): string {
  const international = format?.international.trim();
  return international
    ? `${lead} with its country code, for example ${international}.`
    : `${lead} with its country code, starting with +.`;
}

// The installation's number format from the console context, for screens that
// do not load settings. It is read when the screen opens, so a country saved
// in Settings shows up the next time the screen is opened.
export function useNumberFormat(client: AdminAPIClient): NumberFormat | null {
  const [format, setFormat] = useState<NumberFormat | null>(null);
  useEffect(() => {
    let current = true;
    client.context()
      .then((context) => { if (current) setFormat(contextNumberFormat(context?.send)); })
      .catch(() => undefined);
    return () => { current = false; };
  }, [client]);
  return format;
}

export const COUNTRY_HELP = 'Fax numbers typed without a country code are read as numbers in this country.';

type Option = { value: string; label: string };

// Typing a name or a code finds the country, so GB and US work as well as names.
const filterCountries = createFilterOptions<Option>({ stringify: (option) => `${option.label} ${option.value}` });

// A searchable list of countries by name. Settings labels it with its own row
// label (labelledBy); the Setup Wizard shows the label on the field.
export function CountryField({ value, countries, onChange, disabled, id, label, labelledBy, describedBy, helperText, size }: {
  value: string;
  countries: readonly string[];
  onChange: (code: string) => void;
  disabled?: boolean;
  id?: string;
  label?: string;
  labelledBy?: string;
  describedBy?: string;
  helperText?: string;
  size?: 'small' | 'medium';
}) {
  const options = useMemo(
    () => countryOptions(!value || countries.includes(value) ? countries : [...countries, value]),
    [countries, value],
  );
  const selected = options.find((option) => option.value === value) ?? null;
  return (
    <Autocomplete<Option, false, true, false>
      id={id}
      options={options}
      value={selected as Option}
      onChange={(_, option) => { if (option) onChange(option.value); }}
      getOptionLabel={(option) => option.label}
      filterOptions={filterCountries}
      isOptionEqualToValue={(option, chosen) => option.value === chosen.value}
      disableClearable
      autoHighlight
      disabled={disabled}
      size={size}
      fullWidth
      renderInput={(params) => (
        <TextField
          {...params}
          label={label}
          helperText={helperText}
          inputProps={{
            ...params.inputProps,
            ...(labelledBy ? { 'aria-labelledby': labelledBy } : {}),
            ...(describedBy ? { 'aria-describedby': describedBy } : {}),
          }}
          sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, backgroundColor: 'background.paper' } }}
        />
      )}
    />
  );
}
