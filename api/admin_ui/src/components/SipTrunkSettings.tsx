import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Fade,
  FormControl,
  FormControlLabel,
  FormLabel,
  InputLabel,
  Link,
  MenuItem,
  Radio,
  RadioGroup,
  Select,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../api/client';
import type { SettingsPatch } from '../api/types';
import type { SipCallRecord, SipPreset, SipTrunkSettings as TrunkValues, SipTrunkStatus } from '../api/sipTypes';
import SecretInput from './common/SecretInput';

interface SipTrunkSettingsProps {
  client: AdminAPIClient;
  // The setup wizard shows the trunk form without the call history.
  showCalls?: boolean;
}

type Notice = { severity: 'success' | 'info' | 'warning' | 'error'; text: string } | null;

const E164 = /^\+[1-9][0-9]{6,14}$/;

const EMPTY: TrunkValues = {
  preset: '', auth: 'registration', host: '', port: 0, transport: '', username: '', password: '',
  password_set: false, outbound_proxy: '', caller_id: '', dids: [], t38_enabled: true,
  fax_preference_header: false, codecs: '', external_address: '',
};

const RESULT_TEXT: Record<SipCallRecord['disposition'], string> = {
  answered: 'Answered',
  busy: 'Busy',
  congestion: 'Network busy',
  failed: 'Failed',
  no_answer: 'No answer',
  ambiguous: 'Not known yet',
};

export function connectedTime(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return 'Not known';
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
}

function failure(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError) {
    if (error.status === 403) return 'You do not have permission to do this.';
    if (error.status === 400 && error.detail) return error.detail;
    if (error.status === 409) return 'Settings changed elsewhere. Reload and try again.';
  }
  return fallback;
}

function SipTrunkSettings({ client, showCalls = true }: SipTrunkSettingsProps) {
  const theme = useTheme();
  const narrow = useMediaQuery(theme.breakpoints.down('md'));
  const [presets, setPresets] = useState<SipPreset[]>([]);
  const [saved, setSaved] = useState<TrunkValues>(EMPTY);
  const [form, setForm] = useState<TrunkValues>(EMPTY);
  const [revision, setRevision] = useState<string | undefined>();
  const [didEntry, setDidEntry] = useState('');
  const [notice, setNotice] = useState<Notice>(null);
  const [status, setStatus] = useState<SipTrunkStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [calls, setCalls] = useState<SipCallRecord[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [callsNote, setCallsNote] = useState<string | null>(null);

  const preset = useMemo(() => presets.find((item) => item.id === form.preset), [presets, form.preset]);

  const load = useCallback(async () => {
    try {
      const [catalog, settings] = await Promise.all([client.getSipPresets(), client.getSettings()]);
      setPresets(catalog.presets);
      const trunk = { ...EMPTY, ...((settings.sip as { trunk?: TrunkValues } | undefined)?.trunk ?? {}), password: '' };
      setSaved(trunk);
      setForm(trunk);
      setRevision(settings._meta?.desired_revision_id);
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'Trunk settings could not be loaded. Try again.') });
    }
  }, [client]);

  const loadCalls = useCallback(async (cursor: string | null = null) => {
    try {
      const page = await client.listSipCalls({ cursor, limit: 10 });
      setCalls((current) => (cursor ? [...current, ...page.items] : page.items));
      setNextCursor(page.next_cursor);
      setCallsNote(null);
    } catch (error) {
      setCallsNote(isForbidden(error) ? 'You do not have access to call history.' : 'Recent calls could not be loaded.');
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (showCalls) void loadCalls(); }, [showCalls, loadCalls]);

  const update = <K extends keyof TrunkValues>(key: K, value: TrunkValues[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  const choosePreset = (id: string) => {
    const next = presets.find((item) => item.id === id);
    setForm((current) => ({
      ...current,
      preset: id,
      auth: next && !next.auth_modes.includes(current.auth) ? next.auth_modes[0] : current.auth,
    }));
  };

  const addDid = () => {
    const number = didEntry.trim();
    if (!E164.test(number)) {
      setNotice({ severity: 'warning', text: 'Enter the number in international format, for example +15551234567.' });
      return;
    }
    if (!form.dids.includes(number)) update('dids', [...form.dids, number]);
    setDidEntry('');
  };

  const save = async () => {
    if (form.caller_id && !E164.test(form.caller_id)) {
      setNotice({ severity: 'warning', text: 'Enter the caller ID in international format, for example +15551234567.' });
      return;
    }
    const patch: SettingsPatch = { expected_revision_id: revision };
    const fields: Array<[keyof TrunkValues, string]> = [
      ['preset', 'sip_trunk_preset'], ['auth', 'sip_trunk_auth'], ['host', 'sip_trunk_host'],
      ['port', 'sip_trunk_port'], ['transport', 'sip_trunk_transport'], ['username', 'sip_trunk_username'],
      ['outbound_proxy', 'sip_trunk_outbound_proxy'], ['caller_id', 'sip_trunk_caller_id'],
      ['t38_enabled', 'sip_t38_enabled'], ['fax_preference_header', 'sip_fax_preference_header'],
      ['external_address', 'sip_external_address'],
    ];
    for (const [key, name] of fields) {
      if (form[key] !== saved[key]) patch[name] = form[key] as string | number | boolean;
    }
    if (form.dids.join(',') !== saved.dids.join(',')) patch.sip_trunk_dids = form.dids.join(',');
    if (form.password) patch.sip_trunk_password = form.password;
    if (Object.keys(patch).length === 1) {
      setNotice({ severity: 'info', text: 'Nothing to save.' });
      return;
    }
    setBusy(true);
    try {
      const result = await client.updateSettings(patch);
      setNotice({
        severity: 'success',
        text: result._meta.apply_state === 'pending_restart'
          ? 'Saved. Restart Faxbot, then apply the trunk to Asterisk.'
          : 'Saved. Apply the trunk to Asterisk to use it.',
      });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'The trunk settings could not be saved. Try again.') });
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    setBusy(true);
    try {
      const result = await client.applySipTrunk();
      setNotice({ severity: 'success', text: result.message });
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'The trunk could not be applied to Asterisk. Try again.') });
    } finally {
      setBusy(false);
    }
  };

  const checkStatus = async () => {
    setBusy(true);
    try {
      setStatus(await client.getSipStatus());
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'Trunk status is not available right now.') });
    } finally {
      setBusy(false);
    }
  };

  const statusSeverity = status?.message === 'The trunk is ready.' ? 'success'
    : status && (status.registration === 'rejected' || status.reachability === 'unreachable') ? 'error' : 'info';
  const needsHost = !!preset && (preset.needs_host || preset.id === 'custom');
  const prefixLogin = !!preset?.ip_dial_prefix && form.auth === 'ip';

  return (
    <Stack spacing={2} data-testid="sip-trunk-settings">
      <Typography variant="h6">Carrier SIP trunk</Typography>
      <Typography variant="body2" color="text.secondary">
        Send and receive faxes with Faxbot's own fax engine over your carrier account. Your carrier bills these calls by the minute.
      </Typography>

      <FormControl fullWidth size="small">
        <InputLabel id="sip-preset-label">Carrier</InputLabel>
        <Select labelId="sip-preset-label" label="Carrier" value={form.preset}
          onChange={(event) => choosePreset(String(event.target.value))}>
          <MenuItem value=""><em>No SIP trunk</em></MenuItem>
          {presets.map((item) => <MenuItem key={item.id} value={item.id}>{item.label}</MenuItem>)}
        </Select>
      </FormControl>

      {preset && (
        <>
          <Box>
            {preset.notes.map((note) => <Typography key={note} variant="body2">{note}</Typography>)}
            {preset.t38 && <Typography variant="body2">{preset.t38}</Typography>}
            {preset.sources.length > 0 && (
              <Typography variant="body2" color="text.secondary">
                Carrier documentation, read {preset.sources[0].read_on}:{' '}
                {preset.sources.map((source, index) => (
                  <span key={source.url}>
                    {index > 0 && ', '}
                    <Link href={source.url} target="_blank" rel="noreferrer">{new URL(source.url).hostname}</Link>
                  </span>
                ))}
              </Typography>
            )}
          </Box>

          <FormControl>
            <FormLabel id="sip-auth-label">How Faxbot signs in to the carrier</FormLabel>
            <RadioGroup row aria-labelledby="sip-auth-label" value={form.auth}
              onChange={(event) => update('auth', event.target.value as TrunkValues['auth'])}>
              {preset.auth_modes.includes('registration') && (
                <FormControlLabel value="registration" control={<Radio />} label="Username and password" />
              )}
              {preset.auth_modes.includes('ip') && (
                <FormControlLabel value="ip" control={<Radio />} label="Server IP address" />
              )}
            </RadioGroup>
          </FormControl>

          <Stack direction={narrow ? 'column' : 'row'} spacing={2}>
            <TextField size="small" fullWidth label="Server" value={form.host}
              required={needsHost} placeholder={preset.host || 'sip.example.com'}
              helperText={needsHost ? 'The SIP server name your carrier gave you.' : `Leave empty to use ${preset.host}.`}
              onChange={(event) => update('host', event.target.value.trim())} />
            <TextField size="small" label="Port" type="number" value={form.port || ''}
              placeholder={String(preset.port)} sx={{ minWidth: 120 }}
              onChange={(event) => update('port', Number(event.target.value) || 0)} />
            <FormControl size="small" sx={{ minWidth: 140 }}>
              <InputLabel id="sip-transport-label">Transport</InputLabel>
              <Select labelId="sip-transport-label" label="Transport" value={form.transport}
                onChange={(event) => update('transport', String(event.target.value))}>
                <MenuItem value="">{`Default (${preset.transport.toUpperCase()})`}</MenuItem>
                <MenuItem value="udp">UDP</MenuItem>
                <MenuItem value="tcp">TCP</MenuItem>
                <MenuItem value="tls">TLS</MenuItem>
              </Select>
            </FormControl>
          </Stack>

          {(form.auth === 'registration' || prefixLogin) && (
            <Stack direction={narrow ? 'column' : 'row'} spacing={2}>
              <TextField size="small" fullWidth label={prefixLogin ? 'Tech prefix' : 'Username'} value={form.username}
                helperText={prefixLogin ? 'The eight-digit prefix from your Flowroute account.' : undefined}
                onChange={(event) => update('username', event.target.value.trim())} />
              {form.auth === 'registration' && (
                <SecretInput size="small" fullWidth label="Password" value={form.password}
                  placeholder={saved.password_set ? 'Saved; type a new one to replace it' : ''}
                  helperText={saved.password_set ? 'A password is saved.' : 'Not saved yet.'}
                  onChange={(value) => update('password', value)} />
              )}
            </Stack>
          )}

          <TextField size="small" fullWidth label="Outbound proxy (optional)" value={form.outbound_proxy}
            helperText="Only if your carrier asks for one."
            onChange={(event) => update('outbound_proxy', event.target.value.trim())} />

          <TextField size="small" fullWidth label="Public IP address (optional)" value={form.external_address}
            placeholder="203.0.113.10"
            helperText="Only if Asterisk is behind a router or firewall: the address your carrier should send calls and fax data to."
            onChange={(event) => update('external_address', event.target.value.trim())} />

          <TextField size="small" fullWidth label="Caller ID" value={form.caller_id} required
            placeholder="+15551234567"
            helperText="A number your carrier has assigned to you or verified for you. Faxbot never sends any other number."
            onChange={(event) => update('caller_id', event.target.value.trim())} />

          <Box>
            <Typography variant="subtitle2">Fax numbers on this trunk</Typography>
            <Typography variant="body2" color="text.secondary">The numbers your carrier sends to this trunk.</Typography>
            <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', my: 1 }} useFlexGap>
              {form.dids.length === 0 && <Typography variant="body2">No numbers yet.</Typography>}
              {form.dids.map((number) => (
                <Chip key={number} label={number}
                  onDelete={() => update('dids', form.dids.filter((item) => item !== number))} />
              ))}
            </Stack>
            <Stack direction="row" spacing={1}>
              <TextField size="small" label="Add a number" value={didEntry} placeholder="+15551234567"
                onChange={(event) => setDidEntry(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addDid(); } }} />
              <Button variant="outlined" onClick={addDid}>Add</Button>
            </Stack>
          </Box>

          <FormControlLabel
            control={<Switch checked={form.t38_enabled} onChange={(event) => update('t38_enabled', event.target.checked)} />}
            label="Use T.38 fax over IP (recommended)" />
          <FormControlLabel
            control={<Switch checked={form.fax_preference_header}
              onChange={(event) => update('fax_preference_header', event.target.checked)} />}
            label="Mark outgoing calls as fax when they start" />
          <Typography variant="body2" color="text.secondary" sx={{ mt: -1 }}>
            Some carriers use this to pick a fax-capable route; others ignore it. Faxbot never calls again because of it.
          </Typography>
        </>
      )}

      <Stack direction={narrow ? 'column' : 'row'} spacing={1}>
        <Button variant="contained" onClick={save} disabled={busy}>Save trunk settings</Button>
        <Button variant="outlined" onClick={apply} disabled={busy || !saved.preset}>Apply to Asterisk</Button>
        <Button variant="outlined" onClick={checkStatus} disabled={busy}>Check trunk status</Button>
      </Stack>

      <Fade in={!!notice} unmountOnExit>
        <Alert severity={notice?.severity ?? 'info'} onClose={() => setNotice(null)}>{notice?.text}</Alert>
      </Fade>

      <Fade in={!!status} unmountOnExit>
        <Alert severity={statusSeverity} onClose={() => setStatus(null)} data-testid="sip-trunk-status">
          <Typography variant="body2" fontWeight={600}>{status?.message}</Typography>
          {status?.configured && status.applied && (
            <>
              <Typography variant="body2">{status.registration_text}</Typography>
              <Typography variant="body2">{status.reachability_text}</Typography>
            </>
          )}
        </Alert>
      </Fade>

      {showCalls && (
        <Box>
          <Typography variant="subtitle1" sx={{ mt: 2 }}>Recent calls</Typography>
          {callsNote && <Typography variant="body2" color="text.secondary">{callsNote}</Typography>}
          {!callsNote && calls.length === 0 && <Typography variant="body2" color="text.secondary">No calls yet.</Typography>}
          {calls.length > 0 && (narrow ? (
            <Stack spacing={1}>
              {calls.map((call) => (
                <Card key={call.id} variant="outlined">
                  <CardContent>
                    <Typography variant="body2" fontWeight={600}>
                      {call.direction === 'outbound' ? `Sent to ${call.called ?? 'unknown number'}` : `Received from ${call.caller ?? 'unknown number'}`}
                    </Typography>
                    <Typography variant="body2">{when(call.started_at)}</Typography>
                    <Typography variant="body2">
                      {RESULT_TEXT[call.disposition]}, {connectedTime(call.connected_seconds)}, {call.pages ?? 0} pages, T.38 {call.t38 === 'yes' ? 'yes' : call.t38 === 'no' ? 'no' : 'not known'}
                    </Typography>
                  </CardContent>
                </Card>
              ))}
            </Stack>
          ) : (
            <Table size="small" aria-label="Recent calls">
              <TableHead>
                <TableRow>
                  <TableCell>Time</TableCell>
                  <TableCell>Direction</TableCell>
                  <TableCell>Number</TableCell>
                  <TableCell>Result</TableCell>
                  <TableCell>Connected</TableCell>
                  <TableCell>Pages</TableCell>
                  <TableCell>T.38</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {calls.map((call) => (
                  <TableRow key={call.id}>
                    <TableCell>{when(call.started_at)}</TableCell>
                    <TableCell>{call.direction === 'outbound' ? 'Sent' : 'Received'}</TableCell>
                    <TableCell>{(call.direction === 'outbound' ? call.called : call.caller) ?? 'Unknown'}</TableCell>
                    <TableCell>{RESULT_TEXT[call.disposition]}</TableCell>
                    <TableCell>{connectedTime(call.connected_seconds)}</TableCell>
                    <TableCell>{call.pages ?? '—'}</TableCell>
                    <TableCell>{call.t38 === 'yes' ? 'Yes' : call.t38 === 'no' ? 'No' : 'Not known'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ))}
          {nextCursor && <Button sx={{ mt: 1 }} onClick={() => loadCalls(nextCursor)}>Show older calls</Button>}
        </Box>
      )}
    </Stack>
  );
}

export default SipTrunkSettings;
