import { useId } from 'react';
import { Alert, FormControl, InputLabel, MenuItem, Select, Typography } from '@mui/material';
import type { Settings, SettingsPatch } from '../../api/types';
import { BUILTIN_PROVIDERS, NO_PROVIDER_LABEL, RECEIVING_PROVIDERS, directionSummary, providerLabel } from '../../providerLabels';

// The two provider choices people make, Sending and Receiving, shared by the
// Setup Wizard and Settings. Faxbot stores them as a default provider (the
// sending one), an inbound override only when receiving differs, and whether
// receiving is on.

export interface Directions {
  sending: string;
  receiving: string;
}

export interface PluginProvider {
  id: string;
  name: string;
  categories?: string[];
}

type StoredFields = { backend: string; outbound_backend: string; inbound_backend: string; inbound_enabled: boolean };

export const SENDING_NEEDED = 'Choose a provider for sending as well; Faxbot needs one even when it mainly receives.';

// What sends and what receives faxes now, from the loaded settings.
export function loadedDirections(data: Settings): Directions {
  const fallback = data.backend.type || '';
  const sending = data.hybrid ? data.hybrid.outbound_backend ?? fallback : fallback;
  const receiving = data.inbound.enabled ? (data.hybrid ? data.hybrid.inbound_backend ?? fallback : fallback) : '';
  return { sending: sending || '', receiving: receiving || '' };
}

function loadedFields(data: Settings): StoredFields {
  return {
    backend: data.backend.type || '',
    outbound_backend: data.hybrid?.outbound_override ?? '',
    inbound_backend: data.hybrid?.inbound_override ?? '',
    inbound_enabled: data.inbound.enabled,
  };
}

// The stored provider fields for a choice; the loaded ones when the choice is unchanged.
export function directionFields(choice: Directions, data: Settings): StoredFields {
  const loaded = loadedFields(data);
  const current = loadedDirections(data);
  if (choice.sending === current.sending && choice.receiving === current.receiving) return loaded;
  return {
    backend: choice.sending,
    outbound_backend: '',
    inbound_backend: choice.receiving ? (choice.receiving === choice.sending ? '' : choice.receiving) : loaded.inbound_backend,
    inbound_enabled: !!choice.receiving,
  };
}

// Only the stored provider fields that change for this choice.
export function directionPatch(choice: Directions, data: Settings): SettingsPatch {
  const loaded = loadedFields(data);
  const wanted = directionFields(choice, data);
  const patch: SettingsPatch = {};
  for (const name of Object.keys(wanted) as Array<keyof StoredFields>) {
    if (wanted[name] !== loaded[name]) patch[name] = wanted[name];
  }
  return patch;
}

// Faxbot needs a sending provider whenever it receives.
export function directionProblem(choice: Directions): string | null {
  return !choice.sending && choice.receiving ? SENDING_NEEDED : null;
}

interface ProviderDirectionFieldsProps {
  value: Directions;
  onChange: (next: Directions) => void;
  disabled?: boolean;
  // Installed provider plugins; built-in providers always use their one plain name.
  plugins?: PluginProvider[];
  // The saved choice, so a provider in use stays listed even when it is not offered any more.
  saved?: Directions;
}

export default function ProviderDirectionFields({ value, onChange, disabled, plugins = [], saved }: ProviderDirectionFieldsProps) {
  const id = useId();
  const names = new Map(plugins.map((plugin) => [plugin.id, plugin.name]));
  const label = (provider: string) => providerLabel(provider, names.get(provider));
  const options = (receiving: boolean) => {
    const ids = [...BUILTIN_PROVIDERS, ...plugins.map((plugin) => plugin.id).filter((plugin) => !BUILTIN_PROVIDERS.includes(plugin))];
    for (const known of [value.sending, value.receiving, saved?.sending, saved?.receiving]) {
      if (known && !ids.includes(known)) ids.push(known);
    }
    return ids.filter((provider) => !receiving || RECEIVING_PROVIDERS.has(provider) || provider === value.receiving
      || plugins.some((plugin) => plugin.id === provider && plugin.categories?.includes('inbound')));
  };
  const select = (field: keyof Directions, title: string) => (
    <FormControl fullWidth sx={{ mt: 2 }}>
      <InputLabel id={`${id}-${field}-label`} shrink>{title}</InputLabel>
      <Select id={`${id}-${field}`} labelId={`${id}-${field}-label`} displayEmpty value={value[field]} disabled={disabled}
        label={title} onChange={(event) => onChange({ ...value, [field]: String(event.target.value) })}>
        <MenuItem value="">{NO_PROVIDER_LABEL}</MenuItem>
        {options(field === 'receiving').map((provider) => <MenuItem key={provider} value={provider}>{label(provider)}</MenuItem>)}
      </Select>
    </FormControl>
  );
  const problem = directionProblem(value);
  return (
    <>
      {select('sending', 'Sending')}
      {select('receiving', 'Receiving')}
      {(value.sending || value.receiving) && <Typography sx={{ mt: 2 }}>{directionSummary(value.sending, value.receiving)}</Typography>}
      {problem && <Alert severity="info" sx={{ mt: 1 }}>{problem}</Alert>}
    </>
  );
}
