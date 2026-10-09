// Add an account, or change an extra one: the provider's own fields (for a trunk, the trunk's), plus
// its site, what it does, the numbers it receives on and its limits. Secrets are never shown again;
// leaving a secret empty keeps the saved one.
import { useEffect, useState } from 'react';
import {
  Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle, FormControl, FormControlLabel, InputLabel,
  MenuItem, Select, Stack, TextField, Typography,
} from '@mui/material';
import type { AccountInput, AccountPatch, AccountsState, ProviderAccount, ProviderKind } from './ProviderRulesApi';

const KEY = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const RESERVED = new Set(['local', 'direct']);
const AMOUNT = /^\d+(\.\d{1,6})?$/;

function suggestedKey(provider: string, label: string, taken: Set<string>): string {
  const words = label.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  const base = (words.startsWith(provider) ? words : `${provider}-${words}`).replace(/-+$/, '').slice(0, 32) || provider;
  let candidate = base;
  for (let count = 2; taken.has(candidate); count += 1) candidate = `${base.slice(0, 29)}-${count}`;
  return candidate;
}

const numberList = (text: string) => text.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);

export default function ProviderAccountsDialog({ open, state, account, currency, saving, onClose, onAdd, onChange }: {
  open: boolean;
  state: AccountsState;
  // The extra account to change, or null to add one.
  account: ProviderAccount | null;
  currency: string;
  saving: boolean;
  onClose: () => void;
  onAdd: (input: AccountInput) => void;
  onChange: (key: string, patch: AccountPatch) => void;
}) {
  const [provider, setProvider] = useState('');
  const [label, setLabel] = useState('');
  const [key, setKey] = useState('');
  const [keyTouched, setKeyTouched] = useState(false);
  const [site, setSite] = useState('');
  const [sends, setSends] = useState(true);
  const [receives, setReceives] = useState(true);
  const [numbers, setNumbers] = useState('');
  const [atOnce, setAtOnce] = useState('');
  const [perSecond, setPerSecond] = useState('');
  const [daily, setDaily] = useState('');
  const [fields, setFields] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!open) return;
    setProvider(account?.provider ?? '');
    setLabel(account?.label ?? '');
    setKey(account?.key ?? '');
    setKeyTouched(Boolean(account));
    setSite(account?.site ?? '');
    setSends(account?.sends ?? true);
    setReceives(account?.receives ?? true);
    setNumbers((account?.numbers ?? []).join(', '));
    setAtOnce(account?.limits.at_once ? String(account.limits.at_once) : '');
    setPerSecond(account?.limits.calls_per_second ? String(account.limits.calls_per_second) : '');
    setDaily(account?.limits.daily_limit?.amount ?? '');
    setFields(Object.fromEntries(Object.entries(account?.settings ?? {}).map(([name, value]) => [name, value === null ? '' : String(value)])));
  }, [open, account]);

  const kind: ProviderKind | undefined = state.providers.find((item) => item.id === provider);
  const taken = new Set(state.accounts.map((item) => item.key));
  const trunk = provider === 'sip';
  const canReceive = Boolean(kind?.supports_inbound);
  const shownKey = keyTouched ? key : (provider ? suggestedKey(provider, label || kind?.label || provider, taken) : '');

  const problem = (() => {
    if (!kind) return 'Choose the provider.';
    if (!label.trim()) return 'Give the account a name.';
    if (!account && (!KEY.test(shownKey) || RESERVED.has(shownKey))) return 'Use lower-case letters, digits and dashes for the short name.';
    if (!account && taken.has(shownKey)) return 'Another account already has that short name.';
    for (const field of kind.fields) {
      const value = (fields[field.name] ?? '').trim();
      const kept = account?.secrets_set.includes(field.name);
      if (field.required && !value && !(field.secret && kept)) return `Fill in ${field.label}.`;
    }
    if (daily.trim() && !AMOUNT.test(daily.trim())) return 'Write the daily limit as an amount, such as 25.';
    return null;
  })();

  const limits = () => ({
    at_once: atOnce ? Number(atOnce) : null,
    calls_per_second: trunk && perSecond ? Number(perSecond) : null,
    daily_limit: daily.trim() ? { currency, amount: daily.trim() } : null,
  });
  const settings = () => Object.fromEntries((kind?.fields ?? []).filter((field) => !field.secret)
    .map((field) => [field.name, (fields[field.name] ?? '').trim() || null]));
  const credentials = () => Object.fromEntries((kind?.fields ?? []).filter((field) => field.secret && (fields[field.name] ?? '') !== '')
    .map((field) => [field.name, fields[field.name]]));

  const save = () => {
    if (!kind) return;
    if (account) {
      const patch: AccountPatch = { label: label.trim(), site: site || null, sends, receives: canReceive && receives,
        numbers: numberList(numbers), limits: limits(), settings: settings() };
      const secrets = credentials();
      if (Object.keys(secrets).length > 0) patch.credentials = secrets;
      onChange(account.key, patch);
    } else {
      onAdd({ key: shownKey, provider: kind.id, label: label.trim(), site: site || null, sends, receives: canReceive && receives,
        numbers: numberList(numbers), limits: limits(), settings: settings(), credentials: credentials() });
    }
  };

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm" aria-labelledby="account-dialog-title">
      <DialogTitle id="account-dialog-title">{account ? `Change ${account.label}` : 'Add an account'}</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          {!account && (
            <FormControl size="small" fullWidth>
              <InputLabel id="account-provider">Provider</InputLabel>
              <Select labelId="account-provider" label="Provider" value={provider} inputProps={{ 'aria-label': 'Provider' }}
                onChange={(event) => { setProvider(event.target.value); setFields({}); }}>
                {state.providers.map((item) => <MenuItem key={item.id} value={item.id}>{item.label}</MenuItem>)}
              </Select>
            </FormControl>
          )}
          <TextField size="small" label="Name" value={label} onChange={(event) => setLabel(event.target.value)}
            placeholder={trunk ? 'Leeds trunk (Gamma)' : 'Sinch (UK)'} helperText="What you see in rules, Sent and costs." />
          {!account ? (
            <TextField size="small" label="Short name" value={shownKey}
              onChange={(event) => { setKeyTouched(true); setKey(event.target.value.toLowerCase()); }}
              helperText="Used by the faxbot command, such as sinch-uk. It can't be changed later." />
          ) : (
            <Typography variant="body2" color="text.secondary">Short name: {account.key}</Typography>
          )}
          {state.sites.length > 0 && (
            <FormControl size="small" fullWidth>
              <InputLabel id="account-site">Site</InputLabel>
              <Select labelId="account-site" label="Site" value={site} inputProps={{ 'aria-label': 'Site' }}
                onChange={(event) => setSite(event.target.value)}>
                <MenuItem value="">No site</MenuItem>
                {state.sites.map((item) => <MenuItem key={item.key} value={item.key}>{item.name}</MenuItem>)}
              </Select>
            </FormControl>
          )}
          <Box>
            <FormControlLabel label="Sends faxes" control={<Checkbox checked={sends} onChange={(event) => setSends(event.target.checked)} />} />
            {canReceive && (
              <FormControlLabel label="Receives faxes" control={<Checkbox checked={receives} onChange={(event) => setReceives(event.target.checked)} />} />
            )}
            {kind && !canReceive && (
              <Typography variant="caption" color="text.secondary" display="block">{kind.label} can only send faxes.</Typography>
            )}
          </Box>
          {canReceive && receives && (
            <TextField size="small" label="Fax numbers it receives on" value={numbers} onChange={(event) => setNumbers(event.target.value)}
              helperText="Separate several with commas. Each number belongs to one account." />
          )}
          {kind?.fields.map((field) => (
            <TextField key={field.name} size="small" label={field.label} type={field.secret ? 'password' : 'text'}
              value={fields[field.name] ?? ''} required={field.required && !(field.secret && account?.secrets_set.includes(field.name))}
              autoComplete={field.secret ? 'new-password' : 'off'}
              placeholder={field.secret && account?.secrets_set.includes(field.name) ? 'Saved. Leave empty to keep it.' : undefined}
              helperText={field.help ?? undefined}
              onChange={(event) => setFields({ ...fields, [field.name]: event.target.value })} />
          ))}
          <Typography variant="subtitle2">Limits</Typography>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <TextField size="small" type="number" label={trunk ? 'Lines at once' : 'Faxes at once'} value={atOnce}
              inputProps={{ min: 1 }} onChange={(event) => setAtOnce(event.target.value)} helperText="Empty for no limit." />
            {trunk && (
              <TextField size="small" type="number" label="Calls started each second" value={perSecond}
                inputProps={{ min: 1 }} onChange={(event) => setPerSecond(event.target.value)} helperText="Empty for no limit." />
            )}
            <TextField size="small" label={`Daily spending limit (${currency})`} value={daily} onChange={(event) => setDaily(event.target.value)}
              helperText="Faxbot stops using it until midnight once it reaches this." />
          </Stack>
        </Stack>
      </DialogContent>
      <DialogActions>
        {problem && <Typography variant="body2" color="text.secondary" sx={{ mr: 'auto', pl: 2 }}>{problem}</Typography>}
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={Boolean(problem) || saving} onClick={save}>{account ? 'Save' : 'Add account'}</Button>
      </DialogActions>
    </Dialog>
  );
}
