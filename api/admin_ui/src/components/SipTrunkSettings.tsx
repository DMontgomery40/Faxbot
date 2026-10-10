import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
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
  ListSubheader,
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
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../api/client';
import type { NumberFormat, Settings, SettingsPatch } from '../api/types';
import type { SipCallRecord, SipPreset, SipTrunkSettings as TrunkValues, SipTrunkStatus } from '../api/sipTypes';
import SecretInput from './common/SecretInput';
import { CountryRules } from './delivery/CountryLines';
import LoadFailed, { saysFailure } from './common/LoadFailed';
import EnvSetField, { environmentManaged } from './common/EnvSetField';
import { numberHint, numberPlaceholder, settingsNumberFormat } from './common/numbers';
import InboundRecovery from './InboundRecovery';
import NegotiationSummary from './CallNegotiation';
import NetworkForFax from './NetworkForFax';
import TelnyxT38 from './TelnyxT38';
import TelnyxNames from './TelnyxNames';
import FaxSettings from './FaxSettings';
import { formatServerTime } from '../api/time';
import { TrunkPicker } from './ProviderAccountsTrunks';
import AnalogLinePanel from './AnalogLinePanel';
import CopiersPanel from './CopiersPanel';
import { rulesApiFor } from './ProviderRulesApi';
import TrunkAccountPanel from './TrunkAccountPanel';
import SendOnlyNumbers from './SendOnlyNumbers';

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
  // The carrier or phone system was chosen in the Setup wizard: name it instead of offering every one.
  presetChosenElsewhere?: boolean;
}

type Notice = { severity: 'success' | 'info' | 'warning' | 'error'; text: string } | null;

const EMPTY: TrunkValues = {
  preset: '', auth: 'registration', host: '', port: 0, transport: '', username: '', password: '',
  password_set: false, outbound_proxy: '', caller_id: '', dids: [], t38_enabled: true,
  fax_preference_header: true, codecs: '', external_address: '', dial_format: '', dial_prefix: '',
  own_access: '', public_address_check_minutes: 5,
  // Fax settings: the recommended values.
  t38_error_correction: 'redundancy', t38_max_datagram: 400, fax_max_rate: 14400, fax_ecm: true,
  fax_compression: 'jbig', fax_fine: true, fax_tune_coding: true, sslfax_enabled: true, fax_lines: 2, sslfax_listener_port: 10443,
  max_calls: 0, calls_per_second: 0,
};

// What the phone system section shows: how the phone system reaches Faxbot, from the trunk check.
type Reach = Pick<SipTrunkStatus, 'phone_system' | 'phone_system_command' | 'phone_system_setting'
  | 'phone_system_hidden' | 'ports_text'>;

const isPhoneSystem = (preset?: SipPreset) => preset?.kind === 'phone_system';
// An analog line through a gateway (N8): on the local network like a phone system, with its own wording.
const isAnalogLine = (preset?: SipPreset) => preset?.kind === 'analog_line';
const onLocalNetwork = (preset?: SipPreset) => isPhoneSystem(preset) || isAnalogLine(preset);
const FORMAT_TEXT: Record<string, string> = {
  local_area: 'Ten digits for numbers in this line\'s local calling area, 1 and ten digits for others',
};
const TRANSPORTS_OFFERED: Array<'tls' | 'tcp' | 'udp'> = ['tls', 'tcp', 'udp'];

// A source's read date in the reader's own words ('2026-10-03' is a calendar day, not a moment).
export function readOn(day: string): string {
  const [year, month, date] = day.split('-').map(Number);
  if (!year || !month || !date) return day;
  return new Date(year, month - 1, date).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

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
const ANALOG_INTRO: Record<string, string> = {
  both: 'Send and receive faxes over the business line you already pay for, through a gateway on your local network.',
  receives: 'Receive faxes over the business line you already pay for, through a gateway on your local network.',
  sends: 'Send faxes over the business line you already pay for, through a gateway on your local network.',
};
const PHONE_INTRO: Record<string, string> = {
  both: 'Send and receive faxes with Faxbot\'s own fax engine through your office phone system and its lines.',
  receives: 'Receive faxes with Faxbot\'s own fax engine through your office phone system and its lines.',
  sends: 'Send faxes with Faxbot\'s own fax engine through your office phone system and its lines.',
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
  return formatServerTime(iso, '');
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
export function audioReason(reason: string | null | undefined, at?: string | null, carrier?: string): string | null {
  if (reason === 'no_data_back') {
    const date = at ? new Date(at) : null;
    const day = date && !Number.isNaN(date.getTime())
      ? `on ${date.toLocaleDateString(undefined, { day: 'numeric', month: 'long' })} ` : '';
    return `Off: ${day}a T.38 fax got no fax data back on this network, so Faxbot uses audio fax.`;
  }
  if (reason === 'network') {
    return carrier === 'Telnyx'
      ? 'Off: your network changes port numbers, so fax over IP (T.38) cannot work; Faxbot sends audio fax until the network is fixed.'
      : 'Off: your network changes port numbers, so fax over IP (T.38) most likely cannot work; Faxbot sends audio fax until the network is fixed.';
  }
  if (reason === 'carrier') {
    return `Off: ${carrier || 'your carrier'} turns T.38 into audio fax inside its network, so Faxbot uses audio fax.`;
  }
  if (reason === 'encrypted') {
    return `Off: calls over ${carrier || 'this trunk'} are encrypted here, and fax over IP (T.38) cannot be encrypted, so Faxbot sends encrypted audio fax.`;
  }
  return null;
}

function SipTrunkSettings({ client, showCalls = true, revision: sharedRevision, onSaved, onDirtyChange,
  showReceiving = false, pollMs = 2000, waitMs = 60000, presetChosenElsewhere = false }: SipTrunkSettingsProps) {
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
  const [reach, setReach] = useState<Reach | null>(null);
  // The trunk check behind the received-fax line or the phone system's reach could not be read (said, never hidden).
  const [handoverUnread, setHandoverUnread] = useState(false);
  const [reachUnread, setReachUnread] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  // Several trunks: the trunk account this page shows; null and 'sip' are the first trunk (full page below).
  const [trunkKey, setTrunkKey] = useState<string | null>(null);
  const rules = useMemo(() => rulesApiFor(client), [client]);
  const call = useCallback(<T,>(request: { method: string; path: string; body?: unknown }) => client.call<T>(request),
    [client]);

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
    setHandoverUnread(false);
    client.getSipStatus().then((result) => {
      if (current && result.handover_text) setHandover({ ready: !!result.handover_ready, text: result.handover_text });
    }).catch((failure) => { if (current && saysFailure(failure)) setHandoverUnread(true); });
    return () => { current = false; };
  }, [client, showReceiving]);
  useEffect(() => {
    if (status?.handover_text) {
      setHandover({ ready: !!status.handover_ready, text: status.handover_text });
      setHandoverUnread(false);
    }
  }, [status]);
  // A saved phone system: say at once how it reaches Faxbot, from the same check as trunk status.
  const savedPhone = isPhoneSystem(presets.find((item) => item.id === saved.preset));
  useEffect(() => {
    if (!savedPhone) {
      setReach(null);
      return;
    }
    let current = true;
    setReachUnread(false);
    client.getSipStatus().then((result) => { if (current) setReach(result); })
      .catch((failure) => { if (current && saysFailure(failure)) setReachUnread(true); });
    return () => { current = false; };
  }, [client, savedPhone, saved.preset]);
  useEffect(() => {
    if (status && status.kind === 'phone_system') {
      setReach(status);
      setReachUnread(false);
    }
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
      // A transport or number format the new preset does not offer goes back to its default.
      transport: next && current.transport && !(next.transports ?? TRANSPORTS_OFFERED).includes(current.transport as 'udp')
        ? '' : current.transport,
      dial_format: next && current.dial_format && !(next.dial_formats ?? []).includes(current.dial_format as 'e164')
        ? '' : current.dial_format,
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
      ['external_address', 'sip_external_address'], ['codecs', 'sip_trunk_codecs'],
      ['dial_format', 'sip_trunk_dial_format'], ['dial_prefix', 'sip_trunk_dial_prefix'],
      ['own_access', 'sip_trunk_own_access'],
      ['public_address_check_minutes', 'sip_public_address_check_minutes'],
      // Fax settings.
      ['t38_error_correction', 'sip_t38_error_correction'], ['t38_max_datagram', 'sip_t38_max_datagram'],
      ['fax_max_rate', 'sip_fax_max_rate'], ['fax_ecm', 'sip_fax_ecm'], ['fax_compression', 'sip_fax_compression'],
      ['fax_fine', 'sip_fax_fine'], ['fax_tune_coding', 'sip_fax_tune_coding'],
      ['sslfax_enabled', 'sip_sslfax_enabled'], ['fax_lines', 'sip_fax_lines'],
      ['sslfax_listener_port', 'sip_sslfax_listener_port'],
      // How many calls the trunk takes: faxes beyond them wait for a free line.
      ['max_calls', 'sip_trunk_max_calls'], ['calls_per_second', 'sip_trunk_calls_per_second'],
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
        ? 'The fax engine is restarting to use these settings. Checking the carrier…' : 'Checking the carrier…' });
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
      setNotice({ severity: 'error', text: failure(error, 'Faxbot could not start using these settings. Try again.') });
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

  // Faxbot restarts the fax engine by itself after a fax call it did not answer; this is the same by hand.
  const restartEngine = async () => {
    setBusy(true);
    try {
      const result = await client.restartSipEngine();
      setNotice({ severity: 'success', text: result.message });
      setStatus(await client.getSipStatus());
    } catch (error) {
      setNotice({ severity: 'error', text: failure(error, 'The fax engine could not be restarted. Try again.') });
    } finally {
      setBusy(false);
    }
  };

  // Offered after a T.38 call carried no fax data while T.38 is still on. Faxbot switches new calls to audio
  // fax by itself when such a call timed out waiting for fax data; this does it at once in the other cases.
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

  const offReason = !form.t38_enabled && !saved.t38_enabled
    ? audioReason(saved.t38_off_reason, saved.t38_off_at, presets.find((item) => item.id === saved.preset)?.label) : null;
  const statusSeverity = status?.message === 'The trunk is ready.' ? 'success'
    : status && (status.registration === 'rejected' || status.reachability === 'unreachable'
      || (!!status.ports_text && status.ports_text === status.message)) ? 'error' : 'info';
  const needsHost = !!preset && (preset.needs_host || preset.id === 'custom');
  const sends = use ? use.sends : true;
  const transportInForce = form.transport || preset?.transport || 'udp';
  const portInForce = preset ? (transportInForce === preset.transport ? preset.port : DEFAULT_PORTS[transportInForce]) : 5060;
  const prefixLogin = !!preset?.ip_dial_prefix && form.auth === 'ip';
  const analogLine = isAnalogLine(preset);
  const phone = onLocalNetwork(preset);
  const directions = use?.sends && !use.receives ? 'sends' : use?.receives && !use.sends ? 'receives' : 'both';
  const carriers = presets.filter((item) => !onLocalNetwork(item));
  const phoneSystems = presets.filter(isPhoneSystem);
  const analogLines = presets.filter(isAnalogLine);
  const formats = preset?.dial_formats ?? [];
  const formatValue = form.dial_format || (formats.length === 0 || formats.includes('e164') ? 'e164' : formats[0]);
  const localNumbers = formatValue === 'local' || formatValue === 'local_area';
  // The vendor names the checklist: "What you set in Avaya".
  // A Teams SBC preset names its vendor after the colon: "What you set in AudioCodes Mediant".
  const vendor = preset?.label.includes(': ') ? preset.label.split(': ')[1] : preset?.label.split(' ')[0] ?? '';

  if (trunkKey && trunkKey !== 'sip') {
    return (
      <Stack spacing={2} data-testid="sip-trunk-settings">
        <TrunkPicker api={rules} value={trunkKey} onChange={setTrunkKey} />
        <TrunkAccountPanel api={rules} call={call} accountKey={trunkKey} />
        <AnalogLinePanel call={call} accountKey={trunkKey} />
      </Stack>
    );
  }
  return (
    <Stack spacing={2} data-testid="sip-trunk-settings">
      <TrunkPicker api={rules} value={trunkKey} onChange={setTrunkKey} />
      {Object.values(status?.trunk_problems ?? {}).map((problem) => <Alert key={problem} severity="warning">{problem}</Alert>)}
      {(status?.carrier_notes ?? []).map((note) => <Alert key={note} severity="info">{note}</Alert>)}
      <Typography variant="h6">{analogLine ? 'SIP trunk to your analog line gateway' : phone ? 'SIP trunk to your phone system' : 'Carrier SIP trunk'}</Typography>
      <Typography variant="body2" color="text.secondary">
        {analogLine ? `${ANALOG_INTRO[directions]} Your phone company bills these calls on the line.`
          : phone ? `${PHONE_INTRO[directions]} The carrier behind your phone system bills these calls.`
          : `${INTRO[directions]} Your carrier bills these calls by the minute.`}
      </Typography>

      {presetChosenElsewhere && preset ? (
        <Typography variant="body2" data-testid="sip-preset-chosen">
          {analogLine ? `Analog line gateway: ${preset.label}.` : phone ? `Phone system: ${preset.label}.` : `Carrier: ${preset.id === 'custom' ? 'your own carrier' : preset.label}.`}
          {' '}To use another, choose Add or change a provider.
        </Typography>
      ) : (
      <FormControl fullWidth size="small">
        <InputLabel id="sip-preset-label">Carrier</InputLabel>
        <Select labelId="sip-preset-label" label="Carrier" value={form.preset}
          onChange={(event) => choosePreset(String(event.target.value))}>
          <MenuItem value=""><em>No SIP trunk</em></MenuItem>
          {(phoneSystems.length > 0 || analogLines.length > 0) && <ListSubheader>Carrier</ListSubheader>}
          {carriers.map((item) => <MenuItem key={item.id} value={item.id}>{item.label}</MenuItem>)}
          {phoneSystems.length > 0 && <ListSubheader>Your phone system</ListSubheader>}
          {phoneSystems.map((item) => <MenuItem key={item.id} value={item.id}>{item.label}</MenuItem>)}
          {analogLines.length > 0 && <ListSubheader>Analog line through a gateway</ListSubheader>}
          {analogLines.map((item) => <MenuItem key={item.id} value={item.id}>{item.label}</MenuItem>)}
        </Select>
      </FormControl>
      )}

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

          {!phone && <FormControl>
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
          </FormControl>}

          {phone && (preset.admin_steps ?? []).length > 0 && (
            <Accordion disableGutters variant="outlined" data-testid="phone-system-steps">
              <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                <Typography>{`What you set in ${vendor}`}</Typography>
              </AccordionSummary>
              <AccordionDetails>
                <Box component="ol" sx={{ pl: 3, mt: 0 }}>
                  {(preset.admin_steps ?? []).map((step) => (
                    <li key={step}><Typography variant="body2">{step}</Typography></li>
                  ))}
                </Box>
                {(preset.port_checklist ?? []).length > 0 && (
                  <Box data-testid="teams-port-checklist">
                    <Typography variant="body2" fontWeight={600}>Before the Teams port order</Typography>
                    <Box component="ol" sx={{ pl: 3, mt: 0 }}>
                      {(preset.port_checklist ?? []).map((step) => (
                        <li key={step}><Typography variant="body2">{step}</Typography></li>
                      ))}
                    </Box>
                  </Box>
                )}
                <Typography variant="body2" fontWeight={600}>Sources</Typography>
                {preset.sources.map((source) => (
                  <Typography key={source.url} variant="body2">
                    <Link href={source.url} target="_blank" rel="noreferrer">{new URL(source.url).hostname}</Link>
                    {`, read ${readOn(source.read_on)}`}
                  </Typography>
                ))}
              </AccordionDetails>
            </Accordion>
          )}

          {analogLine && <AnalogLinePanel call={call} accountKey="sip" expectAnalog />}

          <Stack direction={narrow ? 'column' : 'row'} spacing={2}>
            <TextField size="small" fullWidth label={analogLine ? 'Gateway address' : phone ? 'Phone system address' : 'Server'} value={form.host}
              required={needsHost} placeholder={phone ? '192.168.1.10' : preset.host || 'sip.example.com'}
              InputLabelProps={{ shrink: true }}
              helperText={phone ? `${preset.label}'s address on your local network.`
                : needsHost ? 'The server name your carrier gave you.' : `Leave empty to use ${preset.host}.`}
              onChange={(event) => update('host', event.target.value.trim())} />
            <TextField size="small" label="Port" type="number" value={form.port || ''}
              placeholder={String(portInForce)} sx={{ minWidth: 120 }} InputLabelProps={{ shrink: true }}
              helperText={form.port ? undefined : `Leave empty to use ${portInForce}.`}
              onChange={(event) => update('port', Number(event.target.value) || 0)} />
            <FormControl size="small" sx={{ minWidth: 180 }}>
              <InputLabel id="sip-transport-label" shrink>Connection type</InputLabel>
              <Select labelId="sip-transport-label" label="Connection type" value={form.transport} displayEmpty notched
                onChange={(event) => update('transport', String(event.target.value))}>
                <MenuItem value="">{`Default: ${TRANSPORT_TEXT[preset.transport] ?? preset.transport.toUpperCase()}`}</MenuItem>
                {(preset.transports ?? TRANSPORTS_OFFERED).filter((name) => TRANSPORTS_OFFERED.includes(name))
                  .sort((a, b) => TRANSPORTS_OFFERED.indexOf(a) - TRANSPORTS_OFFERED.indexOf(b))
                  .map((name) => <MenuItem key={name} value={name}>{TRANSPORT_TEXT[name]}</MenuItem>)}
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

          {!phone && <TextField size="small" fullWidth label="Outbound proxy (optional)" value={form.outbound_proxy}
            helperText="Only if your carrier asks for one."
            onChange={(event) => update('outbound_proxy', event.target.value.trim())} />}

          {(preset?.encrypted_audio_only || preset?.access_rule) && saved.encryption_sentence && (
            <Alert severity="info" data-testid="trunk-encryption">{saved.encryption_sentence}</Alert>
          )}
          {/* Country rules (the UAE, Saudi Arabia): shown only for accounts in those countries; the server checks writes. */}
          <CountryRules client={client} canWrite />
          {preset?.access_rule && (
            <TextField size="small" fullWidth label="Your own line's internet address" value={form.own_access ?? ''}
              placeholder="203.0.113.7"
              helperText={status?.internet_address
                ? `The address or range of your Telekom line. Faxbot is on ${status.internet_address} now.`
                : 'The address or range of your Telekom line. On any other access Faxbot encrypts the calls by itself.'}
              onChange={(event) => update('own_access', event.target.value.replace(/[^0-9A-Fa-f:./, ]/g, ''))} />
          )}
          {preset?.single_registration && (
            <Typography variant="body2" color="text.secondary">
              {preset.label} allows one registration per account: don't sign in to the same account from a second
              trunk, phone system or standby server.
            </Typography>
          )}

          {!phone && <TextField size="small" fullWidth label="Internet address (optional)" value={form.external_address}
            placeholder="Automatic"
            helperText={status?.internet_address && !form.external_address
              ? `Automatic: Faxbot found ${status.internet_address}. Enter an address only to override it.`
              : 'Leave empty: Faxbot finds its internet address itself and needs no open ports. Enter one only to override it.'}
            onChange={(event) => update('external_address', event.target.value.trim())} />}

          {/* Only an address Faxbot finds itself is checked again. */}
          {!phone && !form.external_address && (
            <TextField size="small" label="Check the internet address every … minutes" type="number"
              value={form.public_address_check_minutes ?? 5} sx={{ maxWidth: 360 }}
              inputProps={{ min: 0, max: 1440 }}
              helperText="How often Faxbot checks whether your internet address changed. Enter 0 to stop checking."
              onChange={(event) => update('public_address_check_minutes',
                Math.min(1440, Math.max(0, Math.round(Number(event.target.value) || 0))))} />
          )}

          {(phone || preset.codecs_by_country) && (
            <FormControl size="small" fullWidth>
              <InputLabel id="sip-codecs-label" shrink>Call sound format</InputLabel>
              <Select labelId="sip-codecs-label" label="Call sound format" value={form.codecs} displayEmpty notched
                onChange={(event) => update('codecs', String(event.target.value))}>
                <MenuItem value="">{'Default: A-law first, or \u00b5-law first in North America and Japan'}</MenuItem>
                <MenuItem value="alaw,ulaw">A-law first (UK, Europe and Australia)</MenuItem>
                <MenuItem value="ulaw,alaw">{'\u00b5-law first (North America)'}</MenuItem>
              </Select>
            </FormControl>
          )}

          {formats.length > 1 && (
            <Stack direction={narrow ? 'column' : 'row'} spacing={2}>
              <FormControl size="small" fullWidth>
                <InputLabel id="sip-dial-format-label">Number format</InputLabel>
                <Select labelId="sip-dial-format-label" label="Number format" value={formatValue}
                  onChange={(event) => update('dial_format', String(event.target.value))}>
                  {formats.includes('e164') && <MenuItem value="e164">
                    {`International, with + and the country code${numberFormat ? ` (${numberFormat.international})` : ''}`}
                  </MenuItem>}
                  <MenuItem value="local">
                    {`As a phone here dials it${numberFormat ? ` (${numberFormat.national})` : ''}`}
                  </MenuItem>
                  {formats.includes('local_area') && <MenuItem value="local_area">{FORMAT_TEXT.local_area}</MenuItem>}
                </Select>
              </FormControl>
              {localNumbers && (
                <TextField size="small" fullWidth label="Outside-line prefix (optional)" value={form.dial_prefix}
                  inputProps={{ inputMode: analogLine ? 'text' : 'numeric', maxLength: analogLine ? 7 : 4 }}
                  helperText={analogLine ? '*70 turns call waiting off for each call Faxbot places, where your exchange offers it.'
                    : phone ? 'Digits your phone system needs before an outside number, such as 9.'
                    : 'Digits your carrier needs before each number, if any.'}
                  onChange={(event) => update('dial_prefix', event.target.value.replace(analogLine ? /[^0-9*]/g : /[^0-9]/g, ''))} />
              )}
            </Stack>
          )}

          <TextField size="small" fullWidth label={sends ? 'Caller ID' : 'Caller ID (optional)'} value={form.caller_id}
            required={sends} type="tel" placeholder={numberPlaceholder(numberFormat)}
            helperText={analogLine
              ? 'The number of the line: your phone company shows it on faxes Faxbot sends over it.'
              : phone
              ? 'The fax number your phone system shows for faxes Faxbot sends.'
              : sends
                ? 'A number your carrier has assigned to you or verified for you. Faxbot never sends any other number.'
                : 'Needed only when the trunk sends faxes: a number your carrier has assigned to you or verified for you.'}
            onChange={(event) => update('caller_id', event.target.value)} />

          <Box>
            <Typography variant="subtitle2">{analogLine ? 'Fax numbers on this line' : phone ? 'Fax numbers your phone system sends to Faxbot' : 'Fax numbers on this trunk'}</Typography>
            <Typography variant="body2" color="text.secondary">
              {numberHint(numberFormat, phone ? 'The fax numbers your phone system routes to Faxbot'
                : 'The numbers your carrier sends to this trunk')}
            </Typography>
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
          <FaxSettings form={form} update={update} />
        </>
      )}

      {phone && reachUnread && !reach && (
        <LoadFailed testId="phone-system-reach-unread"
          text="How your phone system reaches Faxbot could not be checked. Select Check trunk status to try again." />
      )}
      {phone && reach && (reach.ports_text || reach.phone_system_command) && (
        <Box data-testid="phone-system-reach">
          <Typography variant="subtitle2">Reaching Faxbot from your phone system</Typography>
          <Alert severity={reach.phone_system_hidden ? 'error' : reach.phone_system_command ? 'warning' : 'success'}
            sx={{ mt: 1 }}>
            <Typography variant="body2">{reach.ports_text}</Typography>
            {reach.phone_system_command && (
              <>
                <Typography variant="body2" sx={{ mt: 1 }}>
                  {`Set ${reach.phone_system_setting} in .env to this computer's address on your local network, then run:`}
                </Typography>
                <Box component="code" sx={{ display: 'block', mt: 0.5, p: 1, borderRadius: 1, bgcolor: 'action.hover',
                  fontFamily: 'monospace', fontSize: '0.85rem', overflowX: 'auto', whiteSpace: 'pre' }}>
                  {reach.phone_system_command}
                </Box>
              </>
            )}
          </Alert>
        </Box>
      )}

      {!phone && saved.preset && <NetworkForFax client={client} onChanged={load} refresh={status} />}
      {saved.preset === 'telnyx' && <TelnyxT38 client={client} refresh={status} />}
      {saved.preset === 'telnyx' && <TelnyxNames client={client} refresh={status} />}

      <Stack direction={narrow ? 'column' : 'row'} spacing={1}>
        <Button variant="contained" onClick={save} disabled={busy}>Save trunk settings</Button>
        <Button variant="outlined" onClick={applyAndConnect} disabled={busy || !(saved.preset || form.preset)}
          startIcon={connecting ? <CircularProgress size={16} color="inherit" /> : undefined}>Apply and connect</Button>
        <Button variant="outlined" onClick={checkStatus} disabled={busy}>Check trunk status</Button>
      </Stack>

      {showReceiving && handover && (
        <Alert severity={handover.ready ? 'success' : 'warning'} data-testid="sip-handover">{handover.text}</Alert>
      )}
      {showReceiving && !handover && handoverUnread && (
        <LoadFailed testId="sip-handover-unread"
          text="Whether received faxes can reach Faxbot could not be checked. Select Check trunk status to try again." />
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
              {status.engine_text && (
                <Typography variant="body2" data-testid="engine-text">
                  {status.engine_audio ? `${status.engine_text} To try T.38 again, select Apply and connect.`
                    : status.engine_text}
                </Typography>
              )}
              {(status.engine_state === 'running' || status.engine_state === 'starting') && (
                <Button size="small" variant="outlined" sx={{ mt: 1, alignSelf: 'flex-start' }} onClick={restartEngine}
                  disabled={busy}>Restart the fax engine</Button>
              )}
              {status.ports_text && status.ports_text !== status.message && status.kind !== 'phone_system'
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
          <NegotiationSummary client={client} />
        </Box>
      )}
      {showCalls && <SendOnlyNumbers call={call} />}
      {showCalls && <CopiersPanel call={call} />}
    </Stack>
  );
}

export default SipTrunkSettings;
