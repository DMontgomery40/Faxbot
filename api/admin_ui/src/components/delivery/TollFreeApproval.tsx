// Recipients → Details: the recipient's toll-free fax number. Calls to it are paid by the recipient, so Faxbot uses it
// only once someone at the recipient has agreed and you record who, when and the evidence. Each change is kept as a
// new entry; nothing is edited or deleted. The NPI registry (NPPES) can suggest a number; it never approves one.
import { useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, Stack, TextField, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { TollFreeRow, TollFreeState, TollFreeSuggestion } from '../../api/deliveryTypes';
import { formatLocalDate, formatServerTime } from '../../api/time';
import { DeliveryError } from './shared';

const ACTION_WORDS: Record<TollFreeRow['action'], string> = {
  noted: 'Put on file', approved: 'Approved', withdrawn: 'Withdrawn',
};

export const WHO_PAYS = 'The recipient pays for each call to a toll-free number, so Faxbot uses it only after you '
  + 'record who at the recipient agreed.';

function today(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

export default function TollFreeApprovalPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<TollFreeState | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<'none' | 'note' | 'approve'>('none');
  const [tollFree, setTollFree] = useState('');
  const [who, setWho] = useState('');
  const [day, setDay] = useState(today());
  const [evidence, setEvidence] = useState('');
  const [npi, setNpi] = useState('');
  const [suggestions, setSuggestions] = useState<{ sentence: string; items: TollFreeSuggestion[] } | null>(null);
  const [listing, setListing] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    client.getTollFree(number).then((loaded) => { if (live) setView(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;
  const current = view.current && view.current.action !== 'withdrawn' ? view.current : null;

  const record = async (change: Parameters<AdminAPIClient['recordTollFree']>[1]) => {
    setBusy(true);
    setError(null);
    try {
      setView(await client.recordTollFree(number, change));
      setForm('none');
      setWho('');
      setEvidence('');
      setListing(null);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const openApproval = (alternate: string, listed: string | null = null) => {
    setTollFree(alternate);
    setListing(listed);
    setForm('approve');
  };

  const lookUp = async () => {
    setBusy(true);
    setError(null);
    try {
      setSuggestions(await client.lookUpTollFree(number, { npi: npi.trim() }));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box mt={2} data-testid="toll-free-approval">
      <Box display="flex" alignItems="center" gap={1}>
        <Typography variant="subtitle2">Toll-free fax number</Typography>
        {current && <Chip size="small" variant="outlined" color={current.action === 'approved' ? 'success' : 'default'}
          label={current.action === 'approved' ? 'Approved' : 'Not approved yet'} />}
      </Box>
      <Typography variant="body2" sx={{ mt: 0.5 }}>
        {view.sentence ?? 'No toll-free number is on file for this recipient.'}
      </Typography>
      {!current && <Typography variant="body2" color="text.secondary">{WHO_PAYS}</Typography>}
      {current?.action === 'approved' && current.evidence && (
        <Typography variant="body2" color="text.secondary">{`Evidence: ${current.evidence}`}</Typography>
      )}
      <DeliveryError error={error} onClose={() => setError(null)} />

      {canWrite && form === 'none' && (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}>
          {!current && <Button size="small" onClick={() => setForm('note')} disabled={busy}>Add a toll-free number</Button>}
          {current?.action === 'noted' && (
            <Button size="small" onClick={() => openApproval(current.alternate_number)} disabled={busy}>Record approval…</Button>
          )}
          {current && (
            <Button size="small" color="warning" disabled={busy}
              onClick={() => void record({ action: 'withdrawn' })}>
              {current.action === 'approved' ? 'Withdraw approval' : 'Remove from file'}
            </Button>
          )}
        </Stack>
      )}

      {canWrite && form !== 'none' && (
        <Stack spacing={1.5} sx={{ mt: 1.5 }} data-testid="toll-free-form">
          <TextField size="small" label="Toll-free fax number" value={tollFree} disabled={busy}
            onChange={(event) => setTollFree(event.target.value)} placeholder="1-800-555-0100" />
          {form === 'approve' && (
            <>
              <TextField size="small" label="Who at the recipient agreed" value={who} disabled={busy}
                onChange={(event) => setWho(event.target.value)} helperText="For example, Dana, intake lead" />
              <TextField size="small" type="date" label="Day they agreed" value={day} disabled={busy}
                onChange={(event) => setDay(event.target.value)} InputLabelProps={{ shrink: true }} />
              <TextField size="small" label="Evidence" value={evidence} disabled={busy} multiline minRows={2}
                onChange={(event) => setEvidence(event.target.value)}
                helperText={listing ? `Listed in ${listing}. Say where the recipient's agreement is recorded.`
                  : "Where the agreement is recorded, such as an email's subject and date."} />
            </>
          )}
          <Stack direction="row" spacing={1}>
            {form === 'note' && (
              <>
                <Button size="small" variant="contained" disabled={busy || !tollFree.trim()}
                  onClick={() => void record({ action: 'noted', alternate_number: tollFree.trim() })}>Put on file</Button>
                <Button size="small" disabled={busy || !tollFree.trim()} onClick={() => openApproval(tollFree.trim())}>
                  Record approval…
                </Button>
              </>
            )}
            {form === 'approve' && (
              <Button size="small" variant="contained" disabled={busy || !tollFree.trim() || !who.trim() || !day}
                onClick={() => void record({ action: 'approved', alternate_number: tollFree.trim(), approved_by: who.trim(),
                  approved_on: day, evidence: evidence.trim() })}>
                Record approval
              </Button>
            )}
            <Button size="small" disabled={busy} onClick={() => { setForm('none'); setListing(null); }}>Cancel</Button>
          </Stack>
        </Stack>
      )}

      {canWrite && (
        <Box sx={{ mt: 1.5 }}>
          <Typography variant="body2" color="text.secondary">
            For a healthcare provider, the NPI registry (NPPES) may list a toll-free fax number.
          </Typography>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 0.5 }} alignItems={{ sm: 'center' }}>
            <TextField size="small" label="NPI" value={npi} onChange={(event) => setNpi(event.target.value)}
              disabled={busy} inputProps={{ inputMode: 'numeric', maxLength: 10 }} />
            <Button size="small" disabled={busy || npi.trim().length !== 10} onClick={() => void lookUp()}>
              Look up in NPPES
            </Button>
          </Stack>
          {suggestions && (
            <Stack spacing={0.5} sx={{ mt: 1 }} data-testid="toll-free-suggestions">
              <Typography variant="body2">{suggestions.sentence}</Typography>
              {suggestions.items.map((item) => (
                <Stack key={`${item.fax_number}-${item.address_purpose}`} direction={{ xs: 'column', sm: 'row' }} spacing={1}
                  alignItems={{ sm: 'center' }}>
                  <Typography variant="body2">
                    {`${item.fax_display}, ${item.address_purpose}${item.address ? `, ${item.address}` : ''}`}
                  </Typography>
                  <Button size="small" onClick={() => openApproval(item.fax_number, item.evidence)}>Record approval…</Button>
                </Stack>
              ))}
            </Stack>
          )}
        </Box>
      )}

      {view.history.length > 0 && (
        <Box sx={{ mt: 1.5 }}>
          <Typography variant="body2" color="text.secondary">History</Typography>
          {view.history.map((row) => (
            <Typography key={row.id} variant="body2" color="text.secondary">
              {`${formatServerTime(row.recorded_at)}: ${ACTION_WORDS[row.action]} ${row.alternate_display}`
                + (row.approved_by ? `, agreed by ${row.approved_by} on ${formatLocalDate(row.approved_on)}` : '')
                + (row.recorded_by_name ? `, recorded by ${row.recorded_by_name}` : '')}
            </Typography>
          ))}
        </Box>
      )}
      {!canWrite && current === null && view.history.length === 0 && (
        <Alert severity="info" sx={{ mt: 1 }}>You can see toll-free numbers here; recording one needs permission to change settings.</Alert>
      )}
    </Box>
  );
}
