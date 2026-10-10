import { useId } from 'react';
import { Alert, FormControl, InputLabel, ListSubheader, MenuItem, Select, Typography } from '@mui/material';
import type { Settings, SettingsPatch } from '../../api/types';
import { BUILTIN_PROVIDERS, NO_PROVIDER_LABEL, RECEIVING_PROVIDERS, providerLabel } from '../../providerLabels';

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

// The carrier and phone system choices, from GET /admin/sip/presets.
export interface TrunkChoice {
  id: string;
  label: string;
  kind?: 'carrier' | 'phone_system';
}

// Carriers sold in one country, marked so in the list and listed first there.
const CARRIER_REGIONS: Record<string, { country: string; region: string }> = {
  gamma: { country: 'GB', region: 'UK' },
  'bt-one-voice': { country: 'GB', region: 'UK' },
  'telstra-sip-connect': { country: 'AU', region: 'Australia' },
  'swisscom-sbc': { country: 'CH', region: 'Switzerland' },
  'telekom-companyflex': { country: 'DE', region: 'Germany' },
};

// Fax services by the names people know them; Sinch and SignalWire also sell carrier lines.
const SERVICE_NAMES: Record<string, string> = { sinch: 'Sinch Fax', signalwire: 'SignalWire Fax' };
const SERVICES = ['humblefax', 'efax', 'phaxio', 'sinch', 'signalwire', 'documo'];

export interface ChoiceOption { value: string; label: string }
export interface ChoiceGroup { title: string; options: ChoiceOption[] }

// One trunk preset per installation: a carrier or phone system choice is "sip:<preset>".
export function choiceValue(provider: string, preset: string | undefined): string {
  return provider === 'sip' && preset ? `sip:${preset}` : provider;
}

export function readChoice(value: string): { provider: string; preset?: string } {
  return value.startsWith('sip:') ? { provider: 'sip', preset: value.slice(4) } : { provider: value };
}

function carrierLabel(choice: TrunkChoice): string {
  const region = CARRIER_REGIONS[choice.id]?.region;
  return region ? `${choice.label} — ${region}` : choice.label;
}

// Every choice by name, grouped: fax services, your own line through a carrier
// (local carriers first for the installation country), your phone system.
export function providerChoices(trunk: TrunkChoice[] | null, country: string | undefined, receiving: boolean,
  plugins: PluginProvider[] = []): ChoiceGroup[] {
  const groups: ChoiceGroup[] = [];
  const services = SERVICES.filter((provider) => !receiving || RECEIVING_PROVIDERS.has(provider));
  groups.push({ title: 'Fax services', options: services.map((provider) => ({ value: provider, label: SERVICE_NAMES[provider] ?? providerLabel(provider) })) });
  if (trunk && trunk.length) {
    const carriers = trunk.filter((choice) => choice.kind !== 'phone_system' && choice.id !== 'custom');
    const local = carriers.filter((choice) => CARRIER_REGIONS[choice.id]?.country === country);
    const others = carriers.filter((choice) => !local.includes(choice));
    const custom = trunk.find((choice) => choice.id === 'custom');
    groups.push({ title: 'Your own fax line through a carrier', options: [...local, ...others, ...(custom ? [custom] : [])]
      .map((choice) => ({ value: `sip:${choice.id}`, label: carrierLabel(choice) })) });
    const phones = trunk.filter((choice) => choice.kind === 'phone_system');
    if (phones.length) groups.push({ title: 'Your phone system', options: phones.map((choice) => ({ value: `sip:${choice.id}`, label: choice.label })) });
  } else {
    groups.push({ title: 'Your own fax line through a carrier', options: [{ value: 'sip', label: providerLabel('sip') }] });
  }
  const installed = plugins.filter((plugin) => !BUILTIN_PROVIDERS.includes(plugin.id)
    && (!receiving || plugin.categories?.includes('inbound')));
  if (installed.length) groups.push({ title: 'Installed plugins', options: installed.map((plugin) => ({ value: plugin.id, label: plugin.name })) });
  return groups;
}

interface ProviderDirectionFieldsProps {
  value: Directions;
  onChange: (next: Directions) => void;
  disabled?: boolean;
  // Installed provider plugins; built-in providers always use their one plain name.
  plugins?: PluginProvider[];
  // The saved choice, so a provider in use stays listed even when it is not offered any more.
  saved?: Directions;
  // Carriers and phone systems for the trunk; without them the trunk is one choice.
  trunk?: TrunkChoice[] | null;
  // The trunk's carrier or phone system, and a change of it (one for sending and receiving).
  preset?: string;
  onPresetChange?: (preset: string) => void;
  // The installation country, which lists its own carriers first.
  country?: string;
}

export default function ProviderDirectionFields({ value, onChange, disabled, plugins = [], saved, trunk = null, preset = '',
  onPresetChange, country }: ProviderDirectionFieldsProps) {
  const id = useId();
  const names = new Map(plugins.map((plugin) => [plugin.id, plugin.name]));
  const label = (provider: string) => providerLabel(provider, names.get(provider));
  const groups = (receiving: boolean) => {
    const found = providerChoices(trunk, country, receiving, plugins);
    const listed = new Set(found.flatMap((group) => group.options.map((option) => option.value)));
    // A provider in use stays listed even when it is not offered any more.
    const extra = [value.sending, value.receiving, saved?.sending, saved?.receiving]
      .filter((known): known is string => !!known && known !== 'sip' && !listed.has(known));
    return extra.length ? [...found, { title: 'In use now', options: [...new Set(extra)].map((known) => ({ value: known, label: label(known) })) }] : found;
  };
  const current = (field: keyof Directions) => {
    const chosen = value[field];
    if (chosen !== 'sip') return chosen;
    const wanted = choiceValue('sip', preset);
    return groups(field === 'receiving').some((group) => group.options.some((option) => option.value === wanted)) ? wanted : 'sip';
  };
  const choose = (field: keyof Directions, selected: string) => {
    const { provider, preset: chosen } = readChoice(selected);
    onChange({ ...value, [field]: provider });
    if (chosen !== undefined && chosen !== preset) onPresetChange?.(chosen);
  };
  const select = (field: keyof Directions, title: string) => (
    <FormControl fullWidth sx={{ mt: 2 }}>
      <InputLabel id={`${id}-${field}-label`} shrink>{title}</InputLabel>
      <Select id={`${id}-${field}`} labelId={`${id}-${field}-label`} displayEmpty value={current(field)} disabled={disabled}
        label={title} onChange={(event) => choose(field, String(event.target.value))}>
        <MenuItem value="">{NO_PROVIDER_LABEL}</MenuItem>
        {groups(field === 'receiving').flatMap((group) => [
          <ListSubheader key={`${field}-${group.title}`}>{group.title}</ListSubheader>,
          ...group.options.map((option) => <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>),
        ])}
        {current(field) === 'sip' && !groups(field === 'receiving').some((group) => group.options.some((option) => option.value === 'sip'))
          && <MenuItem value="sip">{label('sip')}</MenuItem>}
      </Select>
    </FormControl>
  );
  // The trunk by the carrier or phone system chosen here, before it is saved.
  const nameOf = (provider: string) => {
    if (!provider) return NO_PROVIDER_LABEL;
    const chosen = provider === 'sip' ? trunk?.find((choice) => choice.id === preset) : undefined;
    if (chosen) return chosen.id === 'custom' ? 'Your carrier' : chosen.label;
    return label(provider);
  };
  const problem = directionProblem(value);
  return (
    <>
      {select('sending', 'Sending')}
      {select('receiving', 'Receiving')}
      {(value.sending || value.receiving) && (
        <Typography sx={{ mt: 2 }}>{`Sending: ${nameOf(value.sending)} · Receiving: ${nameOf(value.receiving)}`}</Typography>
      )}
      {problem && <Alert severity="info" sx={{ mt: 1 }}>{problem}</Alert>}
    </>
  );
}
