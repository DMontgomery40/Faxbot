// Providers → In use: the HISP account (Direct messages) and FHIR clients Faxbot can deliver faxes with instead of
// calling, when a recipient can take them. Secrets are write-only: the page says whether each is set, never what
// it is. A FHIR client's public key set is what the recipient's system registers; Faxbot serves it at an address.
import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControl,
  FormControlLabel, InputLabel, MenuItem, Paper, Select, Stack, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../api/client';
import type {
  DigitalAccount, DigitalAccountKind, DigitalAccountsState, DigitalField, DigitalKind,
} from '../api/digitalTypes';
import { formatServerTime } from '../api/time';
import { DeliveryError } from './delivery/shared';

export const DIGITAL_ACCOUNTS_HELP = 'When a recipient can take a Direct message or has a FHIR server, Faxbot can '
  + 'deliver there instead of calling. Add the HISP account and FHIR clients you use here, then confirm each '
  + 'recipient\'s address under Recipients.';
const HEALTH_COLOR = { ready: 'success', not_set_up: 'warning', off: 'default' } as const;
const KIND_WORDS: Record<DigitalKind, string> = { hisp: 'Direct messages (HISP)', fhir: 'FHIR client' };

type Values = Record<string, string | boolean>;

function initialValues(kind: DigitalAccountKind, account: DigitalAccount | null): Values {
  const values: Values = {};
  for (const field of kind.fields) {
    if (field.secret) {
      values[field.name] = '';
      continue;
    }
    const current = account ? account.settings[field.name] : field.default;
    values[field.name] = field.kind === 'bool' ? Boolean(current) : current == null ? '' : String(current);
  }
  return values;
}

function changed(kind: DigitalAccountKind, values: Values, account: DigitalAccount | null) {
  const settings: Record<string, string | boolean | null> = {};
  const credentials: Record<string, string> = {};
  for (const field of kind.fields) {
    const value = values[field.name];
    if (field.secret) {
      if (typeof value === 'string' && value.trim()) credentials[field.name] = value;
      continue;
    }
    const before = account ? account.settings[field.name] : undefined;
    if (field.kind === 'bool') {
      if (!account || Boolean(before) !== value) settings[field.name] = Boolean(value);
    } else if (!account || String(before ?? '') !== String(value)) {
      settings[field.name] = value === '' ? null : String(value);
    }
  }
  return { settings, credentials };
}

function FieldInput({ field, value, busy, set, saved }: {
  field: DigitalField; value: string | boolean; busy: boolean; set: (value: string | boolean) => void; saved: boolean;
}) {
  if (field.kind === 'bool') {
    return <FormControlLabel control={<Checkbox checked={Boolean(value)} disabled={busy}
      onChange={(event) => set(event.target.checked)} />} label={field.label} />;
  }
  if (field.kind === 'choice') {
    return (
      <FormControl size="small">
        <InputLabel id={`digital-${field.name}`}>{field.label}</InputLabel>
        <Select labelId={`digital-${field.name}`} label={field.label} value={String(value)} disabled={busy}
          onChange={(event) => set(String(event.target.value))}>
          {field.choices.map((choice) => <MenuItem key={choice} value={choice}>{choice}</MenuItem>)}
        </Select>
      </FormControl>
    );
  }
  const multiline = field.kind === 'pem' || field.kind === 'lines';
  return (
    <TextField size="small" label={field.label + (field.required ? '' : ' (optional)')} value={String(value)}
      disabled={busy} multiline={multiline} minRows={multiline ? 3 : undefined}
      type={field.secret && !multiline ? 'password' : field.kind === 'date' ? 'date' : 'text'}
      InputLabelProps={field.kind === 'date' ? { shrink: true } : undefined}
      helperText={field.secret && saved ? 'Saved. Leave it empty to keep it.' : field.help ?? undefined}
      onChange={(event) => set(event.target.value)} />
  );
}

function AccountDialog({ state, account, kindId, busy, onClose, onSave }: {
  state: DigitalAccountsState; account: DigitalAccount | null; kindId: DigitalKind; busy: boolean;
  onClose: () => void;
  onSave: (body: { key: string; label: string; settings: Record<string, string | boolean | null>;
    credentials: Record<string, string> }) => void;
}) {
  const kind = state.kinds.find((item) => item.id === kindId) as DigitalAccountKind;
  const [values, setValues] = useState<Values>(() => initialValues(kind, account));
  const [key, setKey] = useState(account?.key ?? kindId);
  const [label, setLabel] = useState(account?.label ?? '');
  const presets = state.presets.filter((preset) => preset.kind === kindId);
  const set = (name: string) => (value: string | boolean) => setValues((current) => ({ ...current, [name]: value }));
  const usePreset = (presetId: string) => {
    const preset = presets.find((item) => item.id === presetId);
    if (!preset) return;
    setValues((current) => ({
      ...current, currency: preset.currency, monthly_fee: preset.monthly_fee ?? '',
      price_per_message: preset.price_per_message ?? '', included_messages: preset.included_messages == null ? ''
        : String(preset.included_messages), price_source: preset.price_source, price_date: preset.price_date,
    }));
  };
  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth aria-labelledby="digital-account-title">
      <DialogTitle id="digital-account-title">
        {account ? `Change ${account.label}` : `Add ${KIND_WORDS[kindId]}`}
      </DialogTitle>
      <DialogContent>
        <Stack spacing={1.5} sx={{ mt: 1 }}>
          {!account && <TextField size="small" label="Key" value={key} disabled={busy}
            helperText="A short name for rules, such as hisp or fhir-hospital." onChange={(event) => setKey(event.target.value)} />}
          <TextField size="small" label="Name" value={label} disabled={busy} onChange={(event) => setLabel(event.target.value)} />
          {kind.fields.map((field) => (
            <FieldInput key={field.name} field={field} value={values[field.name]} busy={busy} set={set(field.name)}
              saved={Boolean(account?.secrets_set.includes(field.name))} />
          ))}
          {presets.length > 0 && (
            <Box>
              <Typography variant="body2" color="text.secondary">Or start the price from a published plan:</Typography>
              {presets.map((preset) => (
                <Button key={preset.id} size="small" disabled={busy} onClick={() => usePreset(preset.id)}>
                  {preset.label}
                </Button>
              ))}
            </Box>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="contained" disabled={busy || !key.trim()}
          onClick={() => onSave({ key: key.trim(), label: label.trim(), ...changed(kind, values, account) })}>
          Save
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export default function DigitalAccounts({ client, canWrite, publicBase }: {
  client: AdminAPIClient;
  canWrite: boolean;
  // Where this Faxbot is reached from outside, for the public key set address; the page's own address otherwise.
  publicBase?: string | null;
}) {
  const [state, setState] = useState<DigitalAccountsState | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ kind: DigitalKind; account: DigitalAccount | null } | null>(null);
  const [bundleFor, setBundleFor] = useState<string | null>(null);
  const [bundle, setBundle] = useState('');

  useEffect(() => {
    let live = true;
    client.getDigitalAccounts().then((loaded) => { if (live) setState(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client]);

  if (!state) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const run = async (work: () => Promise<DigitalAccountsState>, after?: () => void) => {
    setBusy(true);
    setError(null);
    try {
      setState(await work());
      after?.();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const base = (publicBase || window.location.origin).replace(/\/$/, '');

  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, mb: 3 }} data-testid="digital-accounts">
      <Typography variant="h6" component="h2">Direct messages and FHIR</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{DIGITAL_ACCOUNTS_HELP}</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {state.accounts.length === 0 && <Typography variant="body2">No Direct or FHIR account yet.</Typography>}
      {state.accounts.map((account) => (
        <Box key={account.key} sx={{ py: 1, borderTop: 1, borderColor: 'divider' }} data-testid="digital-account">
          <Box display="flex" alignItems="center" gap={1} flexWrap="wrap">
            <Typography variant="subtitle2">{account.label}</Typography>
            <Typography variant="body2" color="text.secondary">{KIND_WORDS[account.provider]}</Typography>
            <Chip size="small" variant="outlined" color={HEALTH_COLOR[account.health.state]}
              label={account.health.state === 'ready' ? 'Ready' : account.health.state === 'off' ? 'Off' : 'Not set up'} />
          </Box>
          <Typography variant="body2">{account.health.sentence}</Typography>
          {account.plan && <Typography variant="body2" color="text.secondary">{account.plan}</Typography>}
          {account.certificate && <Typography variant="body2" color="text.secondary">
            {`Your certificate: ${account.certificate.sentence}`}</Typography>}
          {account.provider === 'hisp' && <Typography variant="body2" color="text.secondary">
            {account.trust_bundle ? `Trust bundle: ${account.trust_bundle.anchors} authorities, loaded `
              + formatServerTime(account.trust_bundle.loaded_at) : 'No trust bundle is loaded yet.'}
          </Typography>}
          {account.provider === 'fhir' && account.public_keys && <Typography variant="body2" color="text.secondary">
            {`Give the recipient's system this public key set address: ${base}/digital/jwks/${account.key}`}
          </Typography>}
          {canWrite && (
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 0.5 }}>
              <Button size="small" disabled={busy} onClick={() => setEditing({ kind: account.provider, account })}>
                Change
              </Button>
              <Button size="small" disabled={busy} onClick={() => void run(() => client.updateDigitalAccount(
                account.key, { enabled: !account.enabled }, state.generation))}>
                {account.enabled ? 'Turn off' : 'Turn on'}
              </Button>
              {account.provider === 'hisp' && (
                <Button size="small" disabled={busy} onClick={() => { setBundleFor(account.key); setBundle(''); }}>
                  Load trust bundle
                </Button>
              )}
              {account.provider === 'fhir' && (
                <Button size="small" disabled={busy} onClick={() => void run(() => client.makeDigitalSigningKey(
                  account.key, null, state.generation))}>
                  {account.public_keys ? 'Make a new signing key' : 'Make a signing key'}
                </Button>
              )}
            </Stack>
          )}
          {bundleFor === account.key && (
            <Stack spacing={1} sx={{ mt: 1 }} data-testid="digital-bundle-form">
              <TextField size="small" label="Trust bundle address, or the bundle itself" value={bundle} disabled={busy}
                multiline minRows={2} onChange={(event) => setBundle(event.target.value)}
                helperText="Your HISP tells you which bundle it belongs to: an https address, or PEM text to paste." />
              <Stack direction="row" spacing={1}>
                <Button size="small" variant="contained" disabled={busy || !bundle.trim()}
                  onClick={() => void run(() => client.loadDigitalTrustBundle(account.key, bundle.trim().startsWith('https://')
                    ? { url: bundle.trim() } : { content: bundle.trim() }), () => setBundleFor(null))}>
                  Load
                </Button>
                <Button size="small" disabled={busy} onClick={() => setBundleFor(null)}>Cancel</Button>
              </Stack>
            </Stack>
          )}
        </Box>
      ))}
      {canWrite && (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}>
          <Button size="small" disabled={busy} onClick={() => setEditing({ kind: 'hisp', account: null })}>
            Add a HISP account
          </Button>
          <Button size="small" disabled={busy} onClick={() => setEditing({ kind: 'fhir', account: null })}>
            Add a FHIR client
          </Button>
        </Stack>
      )}
      {!canWrite && state.accounts.length === 0 && (
        <Alert severity="info" sx={{ mt: 1 }}>Adding an account needs permission to change providers.</Alert>
      )}
      {editing && (
        <AccountDialog state={state} account={editing.account} kindId={editing.kind} busy={busy}
          onClose={() => setEditing(null)}
          onSave={(body) => void run(() => (editing.account
            ? client.updateDigitalAccount(editing.account.key, {
              ...(body.label && body.label !== editing.account.label ? { label: body.label } : {}),
              ...(Object.keys(body.settings).length ? { settings: body.settings } : {}),
              ...(Object.keys(body.credentials).length ? { credentials: body.credentials } : {}),
            }, state.generation)
            : client.addDigitalAccount({ key: body.key, provider: editing.kind, label: body.label || null,
              settings: Object.fromEntries(Object.entries(body.settings).filter(([, value]) => value !== null)) as
                Record<string, string | boolean>, credentials: body.credentials }, state.generation)),
          () => setEditing(null))} />
      )}
    </Paper>
  );
}
