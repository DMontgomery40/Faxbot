// Who settles a sent fax Faxbot could not confirm when its sender cannot, and how soon.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, FormControl, InputLabel, MenuItem, Paper, Select, Stack, TextField, Typography } from '@mui/material';
import type AdminAPIClient from '../../api/client';
import type { CertaintySettings } from '../../api/certaintyTypes';
import { deliveryErrorMessage } from '../delivery/shared';

export default function UncertainSettingsPanel({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [settings, setSettings] = useState<CertaintySettings | null>(null);
  const [hours, setHours] = useState('');
  const [fallback, setFallback] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const show = (next: CertaintySettings) => {
    setSettings(next);
    setHours(String(next.settle_hours));
    setFallback(next.fallback?.id ?? '');
  };
  const load = useCallback(async () => {
    try {
      show(await client.getUncertainSettings());
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }, [client]);
  useEffect(() => { void load(); }, [load]);

  const validHours = /^\d+$/.test(hours) && Number(hours) <= 720;
  const save = async () => {
    if (!settings) return;
    try {
      show(await client.saveUncertainSettings({ fallback_principal_id: fallback || null, settle_hours: Number(hours),
        version: settings.version }));
      setNotice('Saved. Faxes Faxbot finds uncertain from now on use these settings.');
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  };

  return (
    <Paper variant="outlined" sx={{ p: { xs: 2, md: 3 }, borderRadius: 2 }}>
      <Typography variant="h6" sx={{ mb: 1 }}>Sent faxes Faxbot could not confirm</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Each one goes to the person who sent it to settle, or to the backup person of the mailbox it was sent from.
        When neither can see the fax, it goes to the person you choose here.
      </Typography>
      {error ? <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>{deliveryErrorMessage(error)}</Alert> : null}
      {notice ? <Alert severity="success" sx={{ mb: 2 }} onClose={() => setNotice(null)}>{notice}</Alert> : null}
      {settings && (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'flex-start' }}>
          <TextField label="Settle within (hours)" size="small" value={hours} disabled={!canWrite} error={!validHours}
            helperText="0 sets no deadline." inputProps={{ inputMode: 'numeric' }}
            onChange={(event) => setHours(event.target.value.trim())} />
          <FormControl size="small" sx={{ minWidth: 240 }}>
            <InputLabel id="uncertain-fallback">Otherwise, give them to</InputLabel>
            <Select labelId="uncertain-fallback" label="Otherwise, give them to" value={fallback} disabled={!canWrite}
              onChange={(event) => setFallback(String(event.target.value))}>
              <MenuItem value="">Nobody: they wait for you to assign them</MenuItem>
              {settings.people.map((person) => <MenuItem key={person.id} value={person.id}>{person.name}</MenuItem>)}
            </Select>
          </FormControl>
          {canWrite && (
            <Button variant="contained" disabled={!validHours
              || (Number(hours) === settings.settle_hours && fallback === (settings.fallback?.id ?? ''))}
              onClick={() => void save()}>Save</Button>
          )}
        </Stack>
      )}
    </Paper>
  );
}
