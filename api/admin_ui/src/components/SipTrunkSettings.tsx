import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
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
import type { NumberFormat, Settings, SettingsPatch } from '../api/types';
import type { SipCallRecord, SipPreset, SipTrunkSettings as TrunkValues, SipTrunkStatus } from '../api/sipTypes';
import SecretInput from './common/SecretInput';
import EnvSetField, { environmentManaged } from './common/EnvSetField';
import { numberHint, numberPlaceholder, settingsNumberFormat } from './common/numbers';
import InboundRecovery from './InboundRecovery';

interface SipTrunkSettingsProps {
  client: AdminAPIClient;
  // The setup wizard shows the trunk form without the call history.
  showCalls?: boolean;
  // The setup wizard shares its settings revision and hears about saves, so
  // neither form is refused for the other's change.
  revision?: string;
  onSaved?: () => void | Promise<void>;
  // Whether the form has changes that are not saved yet.
  onDirtyChange?: (dirty: boolean) => void;
  // The trunk receives faxes: say whether a received fax can reach Faxbot.
  showReceiving?: boolean;
  // How often and how long Apply and connect checks the trunk after a restart.
  pollMs?: number;
  waitMs?: number;
}

type Notice = { severity: 'success' | 'info' | 'warning' | 'error'; text: string } | null;

const EMPTY: TrunkValues = {
  preset: '', auth: 'registration', host: '', port: 0, transport: '', username: '', password: '',
  password_set: false, outbound_proxy: '', caller_id: '', dids: [], t38_enabled: true,
  fax_preference_header: true, codecs: '', external_address: '',
};

// Plain names for the signaling transport; encrypted is the default for carriers that offer it.
const TRANSPORT_TEXT: Record<string, string> = {
  tls: 'Encrypted (TLS)',
  tcp: 'TCP',
  udp: 'UDP (older)',
};
const DEFAULT_PORTS: Record<string, number> = { udp: 5060, tcp: 5060, tls: 5061 };

// Which directions the trunk carries, from the saved provider choice.
type TrunkUse = { sends: boolean; receives: boolean };

function trunkUse(settings: Settings): TrunkUse {
  const sending = settings.hybrid?.outbound_backend ?? settings.backend.type;
  const receiving = settings.hybrid?.inbound_backend ?? settings.backend.type;
  const routes = String(settings.routing?.outbound_routes ?? '').split(',').map((route) => route.trim());
  return { sends: sending === 'sip' || routes.includes('sip'), receives: !!settings.inbound.enabled && receiving === 'sip' };
}

const INTRO: Record<string, string> = {
  both: 'Send and receive faxes with Faxbot\'s own fax engine over your carrier account.',
  receives: 'Receive faxes with Faxbot\'s own fax engine over your carrier account.',
  sends: 'Send faxes with Faxbot\'s own fax engine over your carrier account.',
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

// Asterisk is back and the carrier has refused Faxbot or answered its check.
function settled(status: SipTrunkStatus): boolean {
  if (status.engine_restarting || !status.asterisk_connected) return false;
  if (status.registration === 'rejected') return true;
  return ['registered', 'not_used'].includes(status.registration) && status.reachability === 'reachable';
}

// Why Faxbot uses audio fax for new calls, in one sentence.
export function audioReason(reason: string | null | undefined, at?: string | null): string | null {
  if (reason === 'no_data_back') {
    const date = at ? new Date(at) : null;
    const day = date && !Number.isNaN(date.getTime())
      ? `on ${date.toLocaleDateString(undefined, { day: 'numeric', month: 'long' })} ` : '';
    return `Off: ${day}a T.38 fax got no fax data back on this network, so Faxbot uses audio fax.`;
  }
  if (reason === 'network') {
    return "Off: your network changes port numbers, and Telnyx's T.38 fax data does not come back through such networks, so Faxbot uses audio fax.";
  }
  return null;
}

function SipTrunkSettings({ client, showCalls = true, revision: sharedRevision, onSaved, onDirtyChange,
  showReceiving = false, pollMs = 2000, waitMs = 60000 }: SipTrunkSettingsProps) {
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
  const [numberFormat, setNumberFormat] = useState<NumberFormat | null>(null);
  const [passwordInEnv, setPasswordInEnv] = useState(false);
  const [use, setUse] = useState<TrunkUse | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [handover, setHandover] = useState<{ ready: boolean; text: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const preset = useMemo(() => presets.find((item) => item.id === form.preset), [presets, form.preset]);
  const expectedRevision = sharedRevision ?? revision;
  const dirty = !!form.password || !!didEntry.trim() || (Object.keys(EMPTY) as Array<keyof TrunkValues>)
    .some((key) => key !== 'password' && key !== 'password_set' && JSON.stringify(form[key]) !== JSON.stringify(saved[key]));
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);

  const load = useCallback(async () => {
    try {
      const [catalog, settings] = await Promise.all([client.getSipPresets(), client.getSettings()]);
      setPresets(catalog.presets);
      const trunk = { ...EMPTY, ...((settings.sip as { trunk?: TrunkValues } | undefined)?.trunk ?? {}), password: '' };
      setSaved(trunk);
      setForm(trunk);
      setRevision(settings._meta?.desired_revision_id);
      setPasswordInEnv(environmentManaged(settings).has('sip_trunk_password'));
      setNumberFormat(settingsNumberFormat(settings));
      setUse(settings.backend && settings.inbound ? trunkUse(settings) : null);
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
  // Received faxes: shown as soon as the form opens, from the same check as trunk status.
  useEffect(() => {
    if (!showReceiving) return;
    let current = true;
    client.getSipStatus().then((result) => {
      if (current && result.handover_text) setHandover({ ready: !!result.handover_ready, text: result.handover_text });
    }).catch(() => undefined);
    return () => { current = false; };
  }, [client, showReceiving]);
  useEffect(() => {
    if (status?.handover_text) setHandover({ ready: !!status.handover_ready, text: status.handover_text });
  }, [status]);
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

  // Numbers are kept as typed until saved; the server saves them in international form.
  const addDid = () => {
    const number = didEntry.trim();
    if (!number) return;
    if (!form.dids.includes(number)) update('dids', [...form.dids, number]);
    setDidEntry('');
  };

  // Save what changed in the form; true when nothing typed is left unsaved.
  const saveForm = async (quiet = false): Promise<boolean> => {
    const patch: SettingsPatch = { expected_revision_id: expectedRevision };
    const fields: Array<[keyof TrunkValues, string]> = [
      ['preset', 'sip_trunk_preset'], ['auth', 'sip_trunk_auth'], ['host', 'sip_trunk_host'],
      ['port', 'sip_trunk_port'], ['transport', 'sip_trunk_transport'], ['username', 'sip_trunk_username'],
      ['outbound_proxy', 'sip_trunk_outbound_proxy'], ['caller_id', 'sip_trunk_caller_id'],
      ['t38_enabled', 'sip_t38_enabled'], ['fax_preference_header', 'sip_fax_preference_header'],
      ['external_address', 'sip_external_address'],
    ];
    // The caller ID keeps its spaces while typed and is trimmed when saved.
    const current: TrunkValues = { ...form, caller_id: form.caller_id.trim() };
    for (const [key, name] of fields) {
      if (current[key] !== saved[key]) patch[name] = current[key] as string | number | boolean;
    }
    if (form.dids.join(',') !== saved.dids.join(',')) patch.sip_trunk_dids = form.dids.join(',');
    if (form.password) patch.sip_trunk_password = form.password;
    if (Object.keys(patch).length === 1) {
      if (!quiet) setNotice({ severity: 'info', text: 'Nothing to save.' });
      return true;
    }
    try {
      const result = await client.updateSettings(patch);
      if (!quiet) {
        setNotice({
          severity: 'success',
          text: result._meta.apply_state === 'pending_restart'
            ? 'Saved. Restart Faxbot, then select Apply and connect.'
            : 'Saved. Select Apply and connect to use it.',
        });
      }
      await load();
      await onSaved?.();
      return true;
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'The trunk settings could not be saved. Try again.') });
      return false;
    }
  };

  const save = async () => {
    setBusy(true);
    try {
      await saveForm();
    } finally {
      setBusy(false);
    }
  };

  // Check the trunk until Asterisk is back and the carrier has answered, or the wait ends.
  const waitForTrunk = async () => {
    const started = Date.now();
    let latest: SipTrunkStatus | null = null;
    while (alive.current) {
      try {
        latest = await client.getSipStatus();
      } catch {
        latest = null;
      }
      if ((latest && settled(latest)) || Date.now() - started >= waitMs) break;
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
    return latest;
  };

  // Write the trunk for Asterisk, let Faxbot restart Asterisk when needed, then show the trunk check.
  const connect = async (savedText?: string) => {
    const result = await client.applySipTrunk();
    if (result.engine === 'restarting' || result.engine === 'current') {
      setConnecting(true);
      setNotice({ severity: 'info', text: result.engine === 'restarting'
        ? 'Asterisk is restarting to use these settings. Checking the carrier…' : 'Checking the carrier…' });
      const latest = await waitForTrunk();
      if (!alive.current) return;
      setConnecting(false);
      setNotice(savedText ? { severity: 'success', text: savedText }
        : result.engine === 'current' ? { severity: 'success', text: result.message } : null);
      if (latest) setStatus(latest);
      else setNotice({ severity: 'error', text: 'Trunk status is not available right now.' });
      return;
    }
    setNotice({ severity: result.engine === 'manual' || !result.engine ? 'success' : 'warning', text: result.message });
  };

  const applyAndConnect = async () => {
    setBusy(true);
    setStatus(null);
    try {
      if (await saveForm(true)) await connect();
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'The trunk could not be applied to Asterisk. Try again.') });
    } finally {
      if (alive.current) {
        setConnecting(false);
        setBusy(false);
      }
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

  // Offered after a T.38 call carried no fax data; Faxbot never changes the mode by itself.
  const useAudioFax = async () => {
    setBusy(true);
    try {
      await client.updateSettings({ expected_revision_id: expectedRevision, sip_t38_enabled: false });
      setStatus(null);
      await load();
      await onSaved?.();
      await connect('New calls send and receive faxes as audio.');
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'Audio fax could not be turned on. Try again.') });
    } finally {
      if (alive.current) {
        setConnecting(false);
        setBusy(false);
      }
    }
  };

  // Turn T.38 back on after Faxbot chose audio fax, and connect the trunk with it.
  const tryT38Again = async () => {
    setBusy(true);
    try {
      await client.updateSettings({ expected_revision_id: expectedRevision, sip_t38_enabled: true });
      setStatus(null);
      await load();
      await onSaved?.();
      await connect('New calls try T.38 again.');
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'T.38 could not be turned back on. Try again.') });
    } finally {
      if (alive.current) {
        setConnecting(false);
        setBusy(false);
      }
    }
  };

  const offReason = !form.t38_enabled && !saved.t38_enabled ? audioReason(saved.t38_off_reason, saved.t38_off_at) : null;
  const statusSeverity = status?.message === 'The trunk is ready.' ? 'success'
    : status && (status.registration === 'rejected' || status.reachability === 'unreachable'
      || (!!status.ports_text && status.ports_text === status.message)) ? 'error' : 'info';
  const needsHost = !!preset && (preset.needs_host || preset.id === 'custom');
  const sends = use ? use.sends : true;
  const transportInForce = form.transport || preset?.transport || 'udp';
  const portInForce = preset ? (transportInForce === preset.transport ? preset.port : DEFAULT_PORTS[transportInForce]) : 5060;
  const prefixLogin = !!preset?.ip_dial_prefix && form.auth === 'ip';

  return (
    <Stack spacing={2} data-testid="sip-trunk-settings">
      <Typography variant="h6">Carrier SIP trunk</Typography>
      <Typography variant="body2" color="text.secondary">
        {`${INTRO[use?.sends && !use.receives ? 'sends' : use?.receives && !use.sends ? 'receives' : 'both']} Your carrier bills these calls by the minute.`}
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
              <Typography variant="body2">
                <Link href={preset.sources[0].url} target="_blank" rel="noreferrer">{preset.label} documentation</Link>
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
              required={needsHost} placeholder={preset.host || 'sip.example.com'} InputLabelProps={{ shrink: true }}
              helperText={needsHost ? 'The SIP server name your carrier gave you.' : `Leave empty to use ${preset.host}.`}
              onChange={(event) => update('host', event.target.value.trim())} />
            <TextField size="small" label="Port" type="number" value={form.port || ''}
              placeholder={String(portInForce)} sx={{ minWidth: 120 }} InputLabelProps={{ shrink: true }}
              helperText={form.port ? undefined : `Leave empty to use ${portInForce}.`}
              onChange={(event) => update('port', Number(event.target.value) || 0)} />
            <FormControl size="small" sx={{ minWidth: 180 }}>
              <InputLabel id="sip-transport-label" shrink>Transport</InputLabel>
              <Select labelId="sip-transport-label" label="Transport" value={form.transport} displayEmpty notched
                onChange={(event) => update('transport', String(event.target.value))}>
                <MenuItem value="">{`Default: ${TRANSPORT_TEXT[preset.transport] ?? preset.transport.toUpperCase()}`}</MenuItem>
                <MenuItem value="tls">{TRANSPORT_TEXT.tls}</MenuItem>
                <MenuItem value="tcp">{TRANSPORT_TEXT.tcp}</MenuItem>
                <MenuItem value="udp">{TRANSPORT_TEXT.udp}</MenuItem>
              </Select>
            </FormControl>
          </Stack>

          {(form.auth === 'registration' || prefixLogin) && (
            <Stack direction={narrow ? 'column' : 'row'} spacing={2}>
              <TextField size="small" fullWidth label={prefixLogin ? 'Tech prefix' : 'Username'} value={form.username}
                helperText={prefixLogin ? 'The eight-digit prefix from your Flowroute account.' : undefined}
                onChange={(event) => update('username', event.target.value.trim())} />
              {form.auth === 'registration' && passwordInEnv && <EnvSetField size="small" fullWidth label="Password" />}
              {form.auth === 'registration' && !passwordInEnv && (
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

          <TextField size="small" fullWidth label="Internet address (optional)" value={form.external_address}
            placeholder="Automatic"
            helperText={status?.internet_address && !form.external_address
              ? `Automatic: Faxbot found ${status.internet_address}. Enter an address only to override it.`
              : 'Leave empty: Faxbot finds its internet address itself and needs no open ports. Enter one only to override it.'}
            onChange={(event) => update('external_address', event.target.value.trim())} />

          <TextField size="small" fullWidth label={sends ? 'Caller ID' : 'Caller ID (optional)'} value={form.caller_id}
            required={sends} type="tel" placeholder={numberPlaceholder(numberFormat)}
            helperText={sends
              ? 'A number your carrier has assigned to you or verified for you. Faxbot never sends any other number.'
              : 'Needed only when the trunk sends faxes: a number your carrier has assigned to you or verified for you.'}
            onChange={(event) => update('caller_id', event.target.value)} />

          <Box>
            <Typography variant="subtitle2">Fax numbers on this trunk</Typography>
            <Typography variant="body2" color="text.secondary">{numberHint(numberFormat, 'The numbers your carrier sends to this trunk')}</Typography>
            <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', my: 1 }} useFlexGap>
              {form.dids.length === 0 && <Typography variant="body2">No numbers yet.</Typography>}
              {form.dids.map((number) => (
                <Chip key={number} label={number}
                  onDelete={() => update('dids', form.dids.filter((item) => item !== number))} />
              ))}
            </Stack>
            <Stack direction="row" spacing={1}>
              <TextField size="small" label="Add a number" value={didEntry} type="tel" placeholder={numberPlaceholder(numberFormat)}
                onChange={(event) => setDidEntry(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addDid(); } }} />
              <Button variant="outlined" onClick={addDid}>Add</Button>
            </Stack>
          </Box>

          <FormControlLabel
            control={<Switch checked={form.t38_enabled} onChange={(event) => update('t38_enabled', event.target.checked)} />}
            label={offReason ? 'Use T.38 fax over IP' : 'Use T.38 fax over IP (recommended)'} />
          {offReason && (
            <Box data-testid="t38-off-reason" sx={{ mt: -1 }}>
              <Typography variant="body2" color="text.secondary">{offReason}</Typography>
              <Button size="small" onClick={tryT38Again} disabled={busy}>Try T.38 again</Button>
            </Box>
          )}
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
        <Button variant="outlined" onClick={applyAndConnect} disabled={busy || !(saved.preset || form.preset)}
          startIcon={connecting ? <CircularProgress size={16} color="inherit" /> : undefined}>Apply and connect</Button>
        <Button variant="outlined" onClick={checkStatus} disabled={busy}>Check trunk status</Button>
      </Stack>

      {showReceiving && handover && (
        <Alert severity={handover.ready ? 'success' : 'warning'} data-testid="sip-handover">{handover.text}</Alert>
      )}

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
          {status?.configured && (
            <>
              {status.public_address_text && <Typography variant="body2">{status.public_address_text}</Typography>}
              {status.ports_text && status.ports_text !== status.message
                && <Typography variant="body2">{status.ports_text}</Typography>}
              {!showReceiving && status.handover_text && status.handover_text !== status.message
                && <Typography variant="body2">{status.handover_text}</Typography>}
              {status.last_call_text && (
                <Typography variant="body2">
                  {`Last call${status.last_call_at ? `, ${when(status.last_call_at)}` : ''}: ${status.last_call_text}`}
                </Typography>
              )}
              {status.suggest_audio && (
                <Box sx={{ mt: 1 }}>
                  <Typography variant="body2">
                    Audio fax may still work when T.38 data cannot come back through your network.
                  </Typography>
                  <Button size="small" variant="outlined" sx={{ mt: 1 }} onClick={useAudioFax} disabled={busy}>
                    Use audio fax for new calls
                  </Button>
                </Box>
              )}
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
                    {call.summary && <Typography variant="body2">{call.summary}</Typography>}
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
                  <TableCell>What happened</TableCell>
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
                    <TableCell>{call.summary ?? ''}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ))}
          {nextCursor && <Button sx={{ mt: 1 }} onClick={() => loadCalls(nextCursor)}>Show older calls</Button>}
          <InboundRecovery client={client} onRecovered={() => { void loadCalls(); }} />
        </Box>
      )}
    </Stack>
  );
}

export default SipTrunkSettings;
