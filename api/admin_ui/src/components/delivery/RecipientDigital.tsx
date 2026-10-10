// Recipients → Details: the recipient's Direct address and FHIR endpoint. Faxbot delivers there instead of calling
// only once you confirm one, and only when it is the better route. The NPI registry (NPPES) can suggest them; a
// suggestion is never used until you confirm it. Each change is kept as a new entry.
import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Chip, FormControl, InputLabel, MenuItem, Select, Stack, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { DigitalAddress, DigitalAddressState, DigitalRecipient } from '../../api/digitalTypes';
import { formatServerTime } from '../../api/time';
import { DeliveryError } from './shared';

const STATE_WORDS: Record<DigitalAddressState, string> = {
  suggested: 'Not confirmed', confirmed: 'Confirmed', withdrawn: 'Withdrawn', dismissed: 'Dismissed',
};
const ACTION_WORDS: Record<DigitalAddressState, string> = {
  suggested: 'Put on file', confirmed: 'Confirmed', withdrawn: 'Withdrawn', dismissed: 'Dismissed',
};

export const DIGITAL_HELP = 'If this recipient can take a Direct message or has a FHIR server, Faxbot can deliver '
  + 'there instead of calling, once you confirm the address with them.';

function source(address: DigitalAddress): string {
  if (address.source === 'nppes') return address.evidence ?? `From the NPI registry, NPI ${address.npi}.`;
  return 'You entered it.';
}

export default function RecipientDigitalPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<DigitalRecipient | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [kind, setKind] = useState<'direct' | 'fhir'>('direct');
  const [address, setAddress] = useState('');
  const [account, setAccount] = useState('');
  const [organization, setOrganization] = useState('');
  const [npi, setNpi] = useState('');

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    client.getDigitalRecipient(number).then((loaded) => { if (live) setView(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const run = async (work: () => Promise<DigitalRecipient>, after?: () => void) => {
    setBusy(true);
    setError(null);
    try {
      setView(await work());
      after?.();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const save = (confirm: boolean) => void run(() => client.addDigitalAddress(number, {
    kind, address: address.trim(), account_key: account || null, organization: organization.trim() || null, confirm,
  }), () => { setAdding(false); setAddress(''); setOrganization(''); });

  const accounts = view.accounts.filter((item) => item.kind === (kind === 'direct' ? 'hisp' : 'fhir'));

  return (
    <Box mt={2} data-testid="recipient-digital">
      <Typography variant="subtitle2">Direct message and FHIR</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }}>{view.sentence}</Typography>
      {view.addresses.length === 0 && <Typography variant="body2" color="text.secondary">{DIGITAL_HELP}</Typography>}
      <DeliveryError error={error} onClose={() => setError(null)} />

      {view.addresses.map((item) => (
        <Box key={item.id} sx={{ mt: 1.5 }} data-testid="digital-address">
          <Box display="flex" alignItems="center" gap={1} flexWrap="wrap">
            <Typography variant="body2">{item.label}</Typography>
            <Chip size="small" variant="outlined" color={item.state === 'confirmed' ? 'success' : 'default'}
              label={STATE_WORDS[item.state]} />
          </Box>
          <Typography variant="body2" color="text.secondary">{item.sentence}</Typography>
          <Typography variant="body2" color="text.secondary">{source(item)}</Typography>
          {canWrite && (
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 0.5 }}>
              {item.state !== 'confirmed' && (
                <Button size="small" disabled={busy}
                  onClick={() => void run(() => client.changeDigitalAddress(number, item.id, 'confirm'))}>
                  Confirm
                </Button>
              )}
              {item.state === 'confirmed' && (
                <Button size="small" color="warning" disabled={busy}
                  onClick={() => void run(() => client.changeDigitalAddress(number, item.id, 'withdraw'))}>
                  Withdraw
                </Button>
              )}
              {item.state === 'suggested' && (
                <Button size="small" disabled={busy}
                  onClick={() => void run(() => client.changeDigitalAddress(number, item.id, 'dismiss'))}>
                  Dismiss
                </Button>
              )}
            </Stack>
          )}
          {item.history.map((entry) => (
            <Typography key={`${item.id}-${entry.recorded_at}-${entry.action}`} variant="caption" color="text.secondary"
              display="block">
              {`${formatServerTime(entry.recorded_at)}: ${ACTION_WORDS[entry.action]}`
                + (entry.recorded_by_name ? ` by ${entry.recorded_by_name}` : '') + (entry.note ? `. ${entry.note}` : '')}
            </Typography>
          ))}
        </Box>
      ))}

      {canWrite && !adding && (
        <Button size="small" sx={{ mt: 1 }} disabled={busy} onClick={() => setAdding(true)}>
          Add a Direct address or FHIR endpoint
        </Button>
      )}
      {canWrite && adding && (
        <Stack spacing={1.5} sx={{ mt: 1.5 }} data-testid="digital-address-form">
          <FormControl size="small">
            <InputLabel id="digital-kind">What it is</InputLabel>
            <Select labelId="digital-kind" label="What it is" value={kind} disabled={busy}
              onChange={(event) => { setKind(event.target.value as 'direct' | 'fhir'); setAccount(''); }}>
              <MenuItem value="direct">Direct address</MenuItem>
              <MenuItem value="fhir">FHIR server</MenuItem>
            </Select>
          </FormControl>
          <TextField size="small" label={kind === 'direct' ? 'Direct address' : 'FHIR server address'} value={address}
            disabled={busy} onChange={(event) => setAddress(event.target.value)}
            placeholder={kind === 'direct' ? 'records@direct.hospital.example' : 'https://fhir.hospital.example/r4'} />
          {accounts.length > 1 && (
            <FormControl size="small">
              <InputLabel id="digital-account">Send with</InputLabel>
              <Select labelId="digital-account" label="Send with" value={account} disabled={busy}
                onChange={(event) => setAccount(String(event.target.value))}>
                <MenuItem value="">The first one</MenuItem>
                {accounts.map((item) => <MenuItem key={item.key} value={item.key}>{item.label}</MenuItem>)}
              </Select>
            </FormControl>
          )}
          {accounts.length === 0 && (
            <Alert severity="info">
              {kind === 'direct' ? 'Add your HISP account under Delivery setup → Providers & accounts before Faxbot can send Direct messages.'
                : 'Add a FHIR client under Delivery setup → Providers & accounts before Faxbot can send to a FHIR server.'}
            </Alert>
          )}
          <TextField size="small" label="Organization (optional)" value={organization} disabled={busy}
            onChange={(event) => setOrganization(event.target.value)} />
          <Stack direction="row" spacing={1}>
            <Button size="small" variant="contained" disabled={busy || !address.trim()} onClick={() => save(true)}>
              Confirm and save
            </Button>
            <Button size="small" disabled={busy || !address.trim()} onClick={() => save(false)}>Save without confirming</Button>
            <Button size="small" disabled={busy} onClick={() => setAdding(false)}>Cancel</Button>
          </Stack>
        </Stack>
      )}

      {canWrite && (
        <Box sx={{ mt: 1.5 }}>
          <Typography variant="body2" color="text.secondary">
            For a healthcare provider, the NPI registry (NPPES) may list a Direct address or FHIR server.
          </Typography>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 0.5 }} alignItems={{ sm: 'center' }}>
            <TextField size="small" label="NPI" value={npi} onChange={(event) => setNpi(event.target.value)}
              disabled={busy} inputProps={{ inputMode: 'numeric', maxLength: 10 }} />
            <Button size="small" disabled={busy || npi.trim().length !== 10}
              onClick={() => void run(() => client.suggestDigitalFromNppes(number, npi.trim()))}>
              Look up in NPPES
            </Button>
          </Stack>
          {view.nppes_sentence && <Typography variant="body2" sx={{ mt: 0.5 }}>{view.nppes_sentence}</Typography>}
        </Box>
      )}
    </Box>
  );
}
