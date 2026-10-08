// Receiving rules on Numbers: the extra conditions and actions a number rule can have (which account a fax
// arrives on, who sent it, when, email, urgency, how long it is kept), and "Try a received fax", which says
// where a fax would go. The number rule list itself stays on Numbers.
import { useState } from 'react';
import {
  Alert, Box, Button, Checkbox, FormControl, FormControlLabel, InputLabel, MenuItem, Paper, Select, Stack, Switch,
  TextField, Typography,
} from '@mui/material';
import { DeliveryError } from './delivery/shared';
import type { ReceivedExplainResult, ReceivingOptions, RulesApi } from './ProviderRulesApi';
import { TimeEditor } from './ProviderRulesEditor';
import { KEEP_DAYS_NOTE, minutesText, textMinutes } from './ProviderRulesText';

export interface Named { key: string; label: string }

function SelectBox({ label, value, options, onChange }: {
  label: string; value: string; options: Array<[string, string]>; onChange: (value: string) => void;
}) {
  const id = `receiving-${label.replace(/\W+/g, '-').toLowerCase()}`;
  return (
    <FormControl size="small" fullWidth>
      <InputLabel id={id}>{label}</InputLabel>
      <Select labelId={id} label={label} value={value} inputProps={{ 'aria-label': label }}
        onChange={(event) => onChange(event.target.value)}>
        {options.map(([option, text]) => <MenuItem key={option} value={option}>{text}</MenuItem>)}
      </Select>
    </FormControl>
  );
}

export const SUBADDRESS_HELP = 'Up to 20 digits some fax machines send with a fax, such as a department\'s extension. '
  + 'The sender\'s machine states it, so it proves nothing about who sent the fax: it only chooses the mailbox and '
  + 'never gives anyone access.';

// The options of one number rule, as fields; the Numbers dialog saves them with the rule.
export function ReceivingOptionsFields({ value, onChange, accounts, connectors, timeZone, sites = [] }: {
  value: ReceivingOptions;
  onChange: (value: ReceivingOptions) => void;
  // Accounts that receive faxes.
  accounts: Named[];
  // Email connectors (Numbers → Email delivery).
  connectors: Named[];
  timeZone: string;
  // Sites (Providers → Rules), for faxes that arrive on an account of one site.
  sites?: Named[];
}) {
  const [fromText, setFromText] = useState(value.from_numbers.join(', '));
  const set = (patch: Partial<ReceivingOptions>) => onChange({ ...value, ...patch });
  const timed = value.days.length > 0 || value.start_minute !== null || value.end_minute !== null;
  const email = value.email_off ? 'off' : value.email_connector_id ?? '';
  return (
    <Stack spacing={2}>
      <FormControlLabel label="This rule is on" control={(
        <Switch checked={value.enabled} onChange={(event) => set({ enabled: event.target.checked })} />
      )} />
      <FormControlLabel label="Use it for faxes to any of your numbers" control={(
        <Checkbox checked={value.any_number} onChange={(event) => set({ any_number: event.target.checked })} />
      )} />
      {accounts.length > 1 && (
        <SelectBox label="Only faxes received on" value={value.account_key ?? ''}
          options={[['', 'Any account'], ...accounts.map((account): [string, string] => [account.key, account.label])]}
          onChange={(key) => set({ account_key: key || null })} />
      )}
      {sites.length > 0 && (
        <SelectBox label="Only faxes received on an account of" value={value.site_key ?? ''}
          options={[['', 'Any site'], ...sites.map((site): [string, string] => [site.key, site.label])]}
          onChange={(key) => set({ site_key: key || null })} />
      )}
      <TextField size="small" label="Only faxes with subaddress" value={value.subaddress ?? ''} placeholder="2001"
        helperText={SUBADDRESS_HELP} inputProps={{ maxLength: 20 }}
        onChange={(event) => set({ subaddress: event.target.value.trim() || null })} />
      <TextField size="small" label="Only faxes from" value={fromText} placeholder="+13035550100, +1303*"
        helperText="Fax numbers, separated by commas. End one with * to match every number that starts with it."
        onChange={(event) => {
          setFromText(event.target.value);
          set({ from_numbers: event.target.value.split(',').map((item) => item.trim()).filter(Boolean) });
        }} />
      <Box>
        <FormControlLabel label={`Only at certain times (${timeZone})`} control={(
          <Checkbox checked={timed} onChange={(event) => set(event.target.checked
            ? { days: ['mon', 'tue', 'wed', 'thu', 'fri'], start_minute: 540, end_minute: 1020 }
            : { days: [], start_minute: null, end_minute: null })} />
        )} />
        {timed && (
          <Box sx={{ pl: 4 }}>
            <TimeEditor label="Received" value={{
              days: value.days,
              from: value.start_minute === null ? undefined : minutesText(value.start_minute),
              until: value.end_minute === null ? undefined : minutesText(value.end_minute),
            }} onChange={(time) => set({
              days: time.days ?? [],
              start_minute: time.from ? textMinutes(time.from) : null,
              end_minute: time.until ? textMinutes(time.until) : null,
            })} />
          </Box>
        )}
      </Box>
      <SelectBox label="Email" value={email}
        options={[['', 'The usual email delivery'], ...connectors.map((connector): [string, string] => [connector.key, `Through ${connector.label}`]),
          ['off', 'No email']]}
        onChange={(choice) => set(choice === 'off' ? { email_off: true, email_connector_id: null }
          : { email_off: false, email_connector_id: choice || null })} />
      <FormControlLabel label="Mark these faxes urgent" control={(
        <Checkbox checked={value.urgent} onChange={(event) => set({ urgent: event.target.checked })} />
      )} />
      <TextField size="small" type="number" label="Keep for (days)" value={value.keep_days ?? ''} inputProps={{ min: 1 }}
        helperText={KEEP_DAYS_NOTE} sx={{ maxWidth: 480 }}
        onChange={(event) => set({ keep_days: event.target.value ? Number(event.target.value) : null })} />
    </Stack>
  );
}

// Try a received fax: which mailbox and email a fax would get. Nothing is saved.
export function ReceivedTry({ api, accounts, timeZone }: { api: RulesApi; accounts: Named[]; timeZone: string }) {
  const [to, setTo] = useState('');
  const [from, setFrom] = useState('');
  const [account, setAccount] = useState('');
  const [subaddress, setSubaddress] = useState('');
  const [at, setAt] = useState('');
  const [result, setResult] = useState<ReceivedExplainResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = () => {
    setError(null);
    api.explainReceived({ to_number: to.trim(), from_number: from.trim() || null, account_key: account || null, at: at || null,
      ...(subaddress.trim() ? { subaddress: subaddress.trim() } : {}) })
      .then(setResult).catch((failure) => { setResult(null); setError(failure); });
  };
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} aria-label="Try a received fax" role="region">
      <Typography variant="h6" component="h2">Try a received fax</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        See where a fax would go. Nothing is received or saved.
      </Typography>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ mb: 2 }}>
        <TextField size="small" label="Sent to your number" value={to} onChange={(event) => setTo(event.target.value)} />
        <TextField size="small" label="From" value={from} onChange={(event) => setFrom(event.target.value)} />
        <TextField size="small" label="Subaddress" value={subaddress} inputProps={{ maxLength: 20 }}
          onChange={(event) => setSubaddress(event.target.value)} />
        {accounts.length > 1 && (
          <Box sx={{ minWidth: 200 }}>
            <SelectBox label="Received on" value={account}
              options={[['', 'Any account'], ...accounts.map((item): [string, string] => [item.key, item.label])]} onChange={setAccount} />
          </Box>
        )}
        <TextField size="small" type="datetime-local" label={`Time at this installation (${timeZone})`} value={at}
          InputLabelProps={{ shrink: true }} helperText="Leave empty for now." onChange={(event) => setAt(event.target.value)} />
      </Stack>
      <Button variant="contained" disabled={!to.trim()} onClick={run}>Try it</Button>
      <Box sx={{ mt: 2 }}>
        <DeliveryError error={error} onClose={() => setError(null)} />
        {result && <Alert severity="info">{result.sentence}</Alert>}
      </Box>
    </Paper>
  );
}
