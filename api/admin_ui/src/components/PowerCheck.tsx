// System → Diagnostics → Power: the UPS Faxbot reads through NUT, so it holds a call the battery could not see
// through (power.py, nut.py). Off until you set the UPS's address; on by itself once set.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Card, CardContent, Stack, TextField, Typography } from '@mui/material';
import { BatteryChargingFull as BatteryIcon } from '@mui/icons-material';
import AdminAPIClient, { AdminAPIError } from '../api/client';
import { siteChecksApi, type Power } from '../api/siteChecks';

const PLAIN = /^[A-Z][^<>{}]{3,300}[.!?]$/;

function refusal(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'Your role cannot change this. Sign in with a role that can change settings.';
  if (error instanceof AdminAPIError && error.detail && PLAIN.test(error.detail.trim())) return error.detail.trim();
  return fallback;
}

export default function PowerCheck({ client }: { client: AdminAPIClient }) {
  const api = siteChecksApi(client);
  const [power, setPower] = useState<Power | null>(null);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  const [form, setForm] = useState({ host: '', port: '3493', ups_name: '', reserve: '2' });

  const show = (found: Power) => {
    setPower(found);
    setForm({ host: found.host ?? '', port: String(found.port), ups_name: found.ups_name ?? '',
      reserve: String(found.reserve_minutes) });
  };

  const load = useCallback(async () => {
    try {
      show(await siteChecksApi(client).power());
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const save = async (host: string) => {
    setNotice(null);
    const port = Number(form.port);
    const reserve = Number(form.reserve);
    if (host && (!Number.isInteger(port) || port < 1 || port > 65535)) {
      setNotice({ severity: 'error', text: 'Enter a port from 1 to 65535; NUT uses 3493.' });
      return;
    }
    if (host && (!Number.isInteger(reserve) || reserve < 0 || reserve > 60)) {
      setNotice({ severity: 'error', text: 'Enter a reserve from 0 to 60 minutes.' });
      return;
    }
    try {
      const saved = await api.setPower(host ? { host, port, ups_name: form.ups_name.trim() || null, reserve_minutes: reserve }
        : { host: '' });
      show(saved);
      setNotice({ severity: 'success', text: host ? 'Saved. Faxbot reads this UPS from now on.' : 'Faxbot no longer reads a UPS.' });
    } catch (error) {
      setNotice({ severity: 'error', text: refusal(error, 'The UPS could not be saved. Try again in a moment.') });
    }
  };

  const severity = power?.status === 'attention' ? 'warning' : power?.status === 'ok' ? 'success' : 'info';

  return (
    <Card sx={{ mb: 3, borderRadius: 2 }} data-testid="power-check">
      <CardContent>
        <Box display="flex" alignItems="center" justifyContent="space-between" gap={1} sx={{ mb: 1 }}>
          <Box display="flex" alignItems="center" gap={1}>
            <BatteryIcon color="action" />
            <Typography variant="h6" component="h2">Power</Typography>
          </Box>
          <Button size="small" onClick={() => void load()}>Check again</Button>
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          With a UPS set, Faxbot starts a call on this server only when the battery can see it through, and
          otherwise sends the fax by a fax service or keeps it waiting in Sent. Nothing changes while the UPS is on
          mains power.
        </Typography>
        {failed && <Alert severity="warning">Faxbot could not read its power settings. Check again in a moment.</Alert>}
        {notice && <Alert severity={notice.severity} sx={{ mb: 1.5 }} onClose={() => setNotice(null)}>{notice.text}</Alert>}
        {power && <Alert severity={severity} sx={{ mb: 1.5 }}>{power.sentence}</Alert>}
        {power && (
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <TextField size="small" label="UPS server address" placeholder="192.168.1.5" value={form.host}
              onChange={(event) => setForm({ ...form, host: event.target.value })} />
            <TextField size="small" label="Port" value={form.port} sx={{ width: 100 }}
              onChange={(event) => setForm({ ...form, port: event.target.value })} />
            <TextField size="small" label="UPS name (optional)" value={form.ups_name}
              onChange={(event) => setForm({ ...form, ups_name: event.target.value })} />
            <TextField size="small" label="Minutes to spare" value={form.reserve} sx={{ width: 140 }}
              onChange={(event) => setForm({ ...form, reserve: event.target.value })} />
            <Button variant="outlined" disabled={!form.host.trim()} onClick={() => void save(form.host.trim())}>Save</Button>
            {power.configured && <Button onClick={() => void save('')}>Stop reading the UPS</Button>}
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}
