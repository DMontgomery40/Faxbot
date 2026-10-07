import { useEffect, useRef, useState } from 'react';
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Card,
  CardContent,
  Typography,
  Button,
  Alert,
  Paper,
  CircularProgress,
  Chip,
  Switch,
  FormControlLabel,
  Stack,
  Link,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
} from '@mui/material';
import {
  Refresh as RefreshIcon,
  Download as DownloadIcon,
  ContentCopy as ContentCopyIcon,
  Security as SecurityIcon,
  Cloud as CloudIcon,
  Storage as StorageIcon,
  CheckCircle as CheckCircleIcon,
  Warning as WarningIcon,
  Error as ErrorIcon,
  Settings as SettingsIcon,
  Public as PublicIcon,
  ExpandMore as ExpandMoreIcon,
} from '@mui/icons-material';
import AdminAPIClient, { AdminAPIError, accessErrorMessage, configurationWriteRejected, isForbidden, plainRefusal } from '../api/client';
import { DELIVERY_SECTIONS, DeliverySettingsSections, EMAIL_DELIVERY_SECTION, deliveryEditorValues, type DeliverySection } from './delivery/DeliverySettings';
import { DEFAULT_DOCS_BASE, docsLink } from '../docsLinks';
import EnvSetField, { ENV_SET_HELP, environmentManaged } from './common/EnvSetField';
import RestartNotice from './common/RestartFaxbot';
import { DeploymentRows } from './common/Deployment';
import type { ConfigurationWriteResult, Settings as SettingsType, SettingsPatch } from '../api/types';
import { ResponsiveSettingItem, ResponsiveSettingSection } from './common/ResponsiveSettingItem';
import { ResponsiveTextField, ResponsiveFormSection } from './common/ResponsiveFormFields';
import SipTrunkSettings from './SipTrunkSettings';
import EfaxSettings, { efaxEditorValues } from './EfaxSettings';
import HumbleFaxReceiving, { humblefaxReceivingValues } from './HumbleFaxReceiving';
import { COUNTRY_HELP, CountryField, countryName, internationalHint, settingsNumberFormat } from './common/numbers';
import { BUILTIN_PROVIDERS, PROVIDER_LABELS, RECEIVING_PROVIDERS, directionSummary, providerLabel } from '../providerLabels';
import ProviderDirectionFields, { directionFields, directionProblem, loadedDirections } from './common/ProviderDirections';

interface SettingsProps {
  client: AdminAPIClient;
  // May this account change settings (email delivery actions are shown only then).
  canWrite?: boolean;
  // May this account restart Faxbot (host:restart); the restart message then offers Restart now.
  canRestart?: boolean;
  // A section to scroll to once settings load, such as the email delivery settings.
  focus?: string | null;
  onFocused?: () => void;
  // Show only these sections, under this page title (a console page shows its own part of the settings).
  sections?: SettingsSection[];
  title?: string;
  // Is this person the installation's owner? Owner-only settings are shown disabled to everyone else.
  isOwner?: boolean;
}

// The parts of the settings document a console page can show on its own.
export type SettingsSection =
  | 'providers' | 'inbound' | 'routes'
  | 'phaxio' | 'sinch' | 'documo' | 'humblefax' | 'efax' | 'trunk' | 'signalwire'
  | 'direct' | 'intake' | 'email'
  | 'security' | 'storage' | 'advanced' | 'backup' | 'mcp' | 'identity'
  | 'plugins' | 'diagnostics' | 'phones' | 'developer' | 'audit' | 'installation-key';

// Sections whose settings each have their own Apply, so one refusal never fails another change.
const OWN_APPLY_SECTIONS = new Set<SettingsSection>(['diagnostics']);

export const OWNER_ONLY_SENTENCE = 'Only the owner of this installation can change this.';

// The server says the same: Sinch's user name counts only with its password, or anyone could send faxes in.
export const SINCH_PASSWORD_NEEDED = 'Enter the password Sinch sends as well; Faxbot does not accept the user name without it.';

/** The sentence to show when a save would leave Sinch's user name without a password; null otherwise. */
export function sinchBasicProblem(form: Record<string, unknown>, fields: string[]): string | null {
  if (!fields.includes('sinch_inbound_basic_user') && !fields.includes('sinch_inbound_basic_pass')) return null;
  const user = String(form.sinch_inbound_basic_user ?? '').trim();
  const password = String(form.sinch_inbound_basic_pass ?? '');
  return user && !password ? SINCH_PASSWORD_NEEDED : null;
}

// One sentence on a provider's own page: whether Faxbot uses it now. The trunk is
// named by its carrier ("Telnyx") once one is chosen.
export function providerUseSentence(provider: string, directions: { sending: string; receiving: string }): string {
  const label = providerLabel(provider);
  const name = provider === 'sip' && label === PROVIDER_LABELS.sip ? 'your phone carrier' : label;
  const sends = directions.sending === provider;
  const receives = directions.receiving === provider;
  if (sends && receives) return `Faxbot sends and receives faxes through ${name}.`;
  if (sends) return `Faxbot sends faxes through ${name}.`;
  if (receives) return `Faxbot receives faxes through ${name}.`;
  return `Faxbot does not use ${name} now. To use it, choose Add or change a provider.`;
}

type FormValue = string | number | boolean;
type SettingsForm = Record<string, FormValue>;

// The trunk section, opened from the Inbox receiving status.
const SIP_TRUNK_SECTION = 'sip-trunk';

const ENV_IMPORT_HELP = 'Decided where Faxbot is installed, not here: when it is on, a fresh installation takes its settings from the recovery copy saved under Storage & retention.';

// Limits checked before saving, so a value the server would refuse gets a plain sentence.
const FIELD_RANGES: Record<string, { min: number; max: number; message: string }> = {
  route_min_success_percent: { min: 0, max: 100, message: 'Enter a minimum delivery rate from 0 to 100.' },
  intake_smtp_port: { min: 1, max: 65535, message: 'Enter an email server port from 1 to 65535.' },
};

// Files Faxbot reads its settings and plugins from, shown read-only in System > Developer.
const READ_ONLY_FILES: Array<{ field: string; label: string; value: (data: SettingsType) => string | undefined }> = [
  { field: 'persisted_env_path', label: 'Recovery .env file', value: (data) => data.persisted?.path },
  { field: 'providers_dir', label: 'Provider plugin folder', value: (data) => data.plugin_files?.providers_dir },
  { field: 'faxbot_config_path', label: 'Older settings file, read when Faxbot was first installed', value: (data) => data.legacy_config?.path },
];

// Each control starts with the loaded settings, including redacted secrets.
// Comparing against this snapshot prevents unrelated edits from writing masks,
// defaults, inactive provider selections, or the opaque database URL.
function editorValues(data: SettingsType): SettingsForm {
  return {
    backend: data.backend.type,
    outbound_backend: data.hybrid?.outbound_override ?? '',
    inbound_backend: data.hybrid?.inbound_override ?? '',
    enforce_public_https: data.security.enforce_https,
    public_api_url: data.security.public_api_url,
    audit_log_enabled: data.security.audit_enabled,
    audit_log_format: data.audit?.format ?? 'json',
    audit_log_file: data.audit?.file ?? '',
    audit_log_syslog: data.audit?.syslog ?? false,
    audit_log_syslog_address: data.audit?.syslog_address ?? '',
    enable_persisted_settings: data.persisted?.enabled ?? false,
    admin_allow_restart: data.restart?.allowed ?? false,
    enable_s3_diagnostics: data.storage.s3_diagnostics ?? false,
    mobile_local_base: data.mobile?.local_base ?? '',
    docs_base_url: data.developer?.docs_base_url ?? '',
    telnyx_api_key: data.sip.telnyx_api_key ?? '',
    feature_v3_plugins: data.features?.v3_plugins ?? false,
    feature_plugin_install: data.features?.plugin_install ?? false,
    fax_disabled: data.backend.disabled,
    inbound_enabled: data.inbound.enabled,
    phaxio_api_key: data.phaxio.api_key,
    phaxio_api_secret: data.phaxio.api_secret,
    phaxio_callback_token: data.phaxio.callback_token,
    phaxio_status_callback_url: data.phaxio.callback_url,
    phaxio_verify_signature: data.phaxio.verify_signature,
    sinch_project_id: data.sinch.project_id,
    sinch_base_url: data.sinch.base_url ?? '',
    sinch_webhook_base_url: data.sinch.webhook_base_url ?? '',
    sinch_api_key: data.sinch.api_key,
    sinch_api_secret: data.sinch.api_secret,
    documo_api_key: data.documo?.api_key ?? '',
    documo_base_url: data.documo?.base_url ?? '',
    documo_use_sandbox: data.documo?.sandbox ?? false,
    humblefax_access_key: data.humblefax?.access_key ?? '',
    humblefax_secret_key: data.humblefax?.secret_key ?? '',
    humblefax_from_number: data.humblefax?.from_number ?? '',
    ...humblefaxReceivingValues(data),
    ...efaxEditorValues(data),
    ami_host: data.sip.ami_host,
    ami_port: data.sip.ami_port,
    ami_username: data.sip.ami_username,
    ami_password: data.sip.ami_password,
    fax_station_id: data.sip.station_id,
    ...(data.sender ? { fax_header: data.sender.header } : {}),
    signalwire_space_url: data.signalwire?.space_url ?? '',
    signalwire_project_id: data.signalwire?.project_id ?? '',
    signalwire_api_token: data.signalwire?.api_token ?? '',
    signalwire_fax_from_e164: data.signalwire?.from_fax ?? '',
    signalwire_sms_from_e164: data.signalwire?.from_sms ?? '',
    signalwire_status_callback_url: data.signalwire?.callback_url ?? '',
    signalwire_webhook_signing_key: data.signalwire?.webhook_signing_key ?? '',
    signalwire_status_poll_seconds: data.signalwire?.status_poll_seconds ?? 0,
    inbound_retention_days: data.inbound.retention_days,
    inbound_token_ttl_minutes: data.inbound.token_ttl_minutes ?? 60,
    asterisk_inbound_secret: data.inbound.sip?.asterisk_secret ?? '',
    phaxio_inbound_verify_signature: data.inbound.phaxio?.verify_signature ?? true,
    sinch_inbound_basic_user: data.inbound.sinch?.basic_user ?? '',
    sinch_inbound_basic_pass: data.inbound.sinch?.basic_pass ?? '',
    storage_backend: data.storage.backend,
    s3_bucket: data.storage.s3_bucket,
    s3_region: data.storage.s3_region ?? '',
    s3_prefix: data.storage.s3_prefix ?? '',
    s3_endpoint_url: data.storage.s3_endpoint_url ?? '',
    s3_kms_key_id: data.storage.s3_kms_key_id ?? '',
    max_file_size_mb: data.limits.max_file_size_mb,
    max_requests_per_minute: data.limits.rate_limit_rpm,
    inbound_list_rpm: data.limits.inbound_list_rpm ?? 30,
    inbound_get_rpm: data.limits.inbound_get_rpm ?? 60,
    pdf_token_ttl_minutes: data.limits.pdf_token_ttl_minutes,
    artifact_ttl_days: data.limits.artifact_ttl_days ?? 0,
    cleanup_interval_minutes: data.limits.cleanup_interval_minutes ?? 1440,
    enable_mcp_sse: data.mcp?.sse_enabled ?? false,
    mcp_sse_path: data.mcp?.sse_path ?? '/mcp/sse',
    enable_mcp_http: data.mcp?.http_enabled ?? false,
    mcp_http_path: data.mcp?.http_path ?? '/mcp/http',
    require_mcp_oauth: data.mcp?.require_oauth ?? false,
    oauth_issuer: data.mcp?.oauth.issuer ?? '',
    oauth_audience: data.mcp?.oauth.audience ?? '',
    oauth_jwks_url: data.mcp?.oauth.jwks_url ?? '',
    ...(data.numbers ? { fax_default_country: data.numbers.default_country } : {}),
    ...deliveryEditorValues(data),
  };
}

function Settings({ client, canWrite = false, canRestart = false, focus, onFocused, sections, title, isOwner = true }: SettingsProps) {
  // Without sections, the whole settings document is shown.
  const shows = (section: SettingsSection) => !sections || sections.includes(section);
  const [settings, setSettings] = useState<SettingsType | null>(null);
  const [envContent, setEnvContent] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [snack, setSnack] = useState<string | null>(null);
  const [form, setForm] = useState<SettingsForm>({});
  const [loadedForm, setLoadedForm] = useState<SettingsForm>({});
  const [docsBase, setDocsBase] = useState<string>(DEFAULT_DOCS_BASE);
  const [lastGeneratedSecret, setLastGeneratedSecret] = useState<string>('');
  const [needsReload, setNeedsReload] = useState(false);
  const [saveResult, setSaveResult] = useState<ConfigurationWriteResult | null>(null);
  // Why sending cannot work right now, such as the fax engine refusing Faxbot's login.
  const [engineMessage, setEngineMessage] = useState<string | null>(null);
  // Whether the installation lets the console restart Faxbot (ADMIN_ALLOW_RESTART).
  const [allowRestart, setAllowRestart] = useState(false);
  const [restarted, setRestarted] = useState(false);
  // A switch change waiting for the person to confirm it, such as turning sending off.
  const [confirming, setConfirming] = useState<{ field: string; value: boolean; title: string; text: string; action: string } | null>(null);
  const actionFence = useRef(false);
  const requestEpoch = useRef(0);
  const desiredRevision = needsReload ? undefined : settings?._meta?.desired_revision_id;
  const canEdit = !!desiredRevision && !loading && !needsReload;
  const handleForm = (field: string, value: FormValue) => {
    if (!canEdit || actionFence.current) return;
    setForm((prev) => ({ ...prev, [field]: value }));
  };
  const revisionMeta = needsReload && saveResult ? saveResult._meta : settings?._meta;
  const pendingRestart = revisionMeta?.apply_state === 'pending_restart';
  const pendingCount = settings?._meta?.pending_fields?.length ?? 0;
  const restartMessage = pendingCount
    ? `Restart Faxbot to apply ${pendingCount} pending ${pendingCount === 1 ? 'change' : 'changes'}.`
    : 'Restart Faxbot to apply pending changes.';
  const loadedOutbound = settings?.hybrid?.outbound_backend ?? settings?.backend.type ?? '';
  const loadedInbound = settings?.hybrid?.inbound_backend ?? settings?.backend.type ?? '';
  const effectiveOutbound = form.outbound_backend || form.backend || loadedOutbound;
  // Credentials supplied by .env: shown as set there, never editable or revealable here.
  const envSet = environmentManaged(settings);
  const envField = (field: string) => (envSet.has(field) ? {
    onChange: undefined,
    showCurrentValue: false,
    helperText: ENV_SET_HELP,
    renderControl: ({ id, labelledBy, describedBy }: { id: string; labelledBy: string; describedBy?: string }) => (
      <EnvSetField id={id} fullWidth size="small" withHelp={false}
        inputProps={{ 'aria-labelledby': labelledBy, 'aria-describedby': describedBy }} />
    ),
  } : {});
  const effectiveInbound = form.inbound_backend || form.backend || loadedInbound;
  // Sending and Receiving, as the Setup Wizard shows them.
  const providerChoice = {
    sending: String(form.outbound_backend || form.backend || ''),
    receiving: form.inbound_enabled ? String(form.inbound_backend || form.backend || '') : '',
  };
  const providerSelected = (provider: string) => effectiveOutbound === provider || effectiveInbound === provider;
  // A provider's own page shows its settings whether or not it is in use, with one sentence saying which.
  const providerShown = (provider: string, section: SettingsSection) => (sections ? sections.includes(section) : providerSelected(provider));
  // On a provider's own page its name is already the page title, so its section is the account.
  const providerTitle = (name: string) => (sections ? 'Account' : name);
  const providerStatus = (provider: string) => (sections && settings ? (
    <Typography variant="body2" sx={{ mb: 2 }} data-testid="provider-use">
      {providerUseSentence(provider, loadedDirections(settings))}
    </Typography>
  ) : null);
  const changedFields = Object.keys(form).filter((field) => form[field] !== loadedForm[field]);

  const hydrate = (data: SettingsType) => {
    const values = editorValues(data);
    setSettings(data);
    setForm(values);
    setLoadedForm(values);
    setLastGeneratedSecret('');
    setNeedsReload(false);
  };

  const applySettings = async (only?: string[]) => {
    if (actionFence.current || loading) return;
    if (!desiredRevision) {
      setError('Select Reload before applying changes.');
      return;
    }
    actionFence.current = true;
    const epoch = requestEpoch.current;
    let writeStarted = false;
    try {
      setLoading(true);
      setError(null);
      setSnack(null);
      const problem = only ? null : directionProblem(providerChoice);
      if (problem) throw new Error(problem);
      const patch: SettingsPatch = { expected_revision_id: desiredRevision };
      const fields = only ? changedFields.filter((field) => only.includes(field)) : changedFields;
      const sinchProblem = sinchBasicProblem(form, fields);
      if (sinchProblem) throw new Error(sinchProblem);
      // Edits to other settings stay on screen after a partial apply.
      const kept = Object.fromEntries(changedFields.filter((field) => !fields.includes(field)).map((field) => [field, form[field]]));
      for (const field of fields) {
        const value = form[field];
        if (typeof loadedForm[field] === 'number' && (value === '' || !Number.isFinite(Number(value)) || !Number.isSafeInteger(Number(value)))) {
          throw new Error('Enter a whole number. An empty box does not clear the setting.');
        }
        patch[field] = typeof loadedForm[field] === 'number' ? Number(value) : value;
        const range = FIELD_RANGES[field];
        if (range && (Number(value) < range.min || Number(value) > range.max)) throw new Error(range.message);
      }
      setSaveResult(null);
      writeStarted = true;
      const writeResult = await client.updateSettings(patch);
      if (epoch !== requestEpoch.current) return;
      setSaveResult(writeResult);
      setNeedsReload(true);
      setEnvContent('');
      setSnack(writeResult._meta.apply_state === 'pending_restart'
        ? `${writeResult.changed ? 'Settings saved.' : 'Nothing changed.'} Restart Faxbot to apply pending changes.`
        : writeResult.changed ? 'Settings saved.' : 'Nothing changed.');
      try {
        const data = await client.getSettings();
        if (epoch !== requestEpoch.current) return;
        if (!data._meta?.desired_revision_id) throw new Error('Settings could not be loaded.');
        hydrate(data);
        if (only) setForm((previous) => ({ ...previous, ...kept }));
      } catch {
        if (epoch !== requestEpoch.current) return;
        setError('The page could not refresh. Select Reload before making more changes.');
      }
    } catch (err) {
      if (epoch !== requestEpoch.current) return;
      // One plain sentence, never the raw "API Error: 500".
      const message = plainRefusal(err) ?? accessErrorMessage(err);
      if (!writeStarted) {
        setError(message);
        return;
      }
      // A partial apply that was refused changed nothing: say why and keep editing.
      if (only && (isForbidden(err) || plainRefusal(err))) {
        setError(isForbidden(err) ? 'You do not have permission to change this setting.' : plainRefusal(err));
        return;
      }
      setNeedsReload(true);
      setError(err instanceof AdminAPIError && err.status === 409
        ? 'Someone else changed these settings. Your edits are kept here; reload to see the current values.'
        : isForbidden(err)
          ? 'You do not have permission to change some of these settings. Your edits are kept here; reload before trying again.'
        : plainRefusal(err)
          ? `${plainRefusal(err)} Your edits are kept here; reload before trying again.`
        : configurationWriteRejected(err)
          ? `Settings were not saved; your edits are kept here. ${message}`
          : 'The save could not be confirmed. Reload to check whether your changes were saved.');
    } finally {
      if (epoch === requestEpoch.current) {
        actionFence.current = false;
        setLoading(false);
      }
    }
  };

  // Settings only the owner may change: shown, but disabled with one sentence, to everyone else.
  const ownerOnly = new Set(settings?.owner_only ?? []);
  const locked = (field: string) => !isOwner && ownerOnly.has(field);
  const withOwnerNote = (field: string, helperText: string) => (locked(field)
    ? [helperText, OWNER_ONLY_SENTENCE].filter(Boolean).join(' ') : helperText);
  const textField = (label: string, field: string, helperText = '', type: 'text' | 'password' | 'number' = 'text') => (
    <ResponsiveSettingItem
      icon={type === 'password' ? <SecurityIcon /> : <SettingsIcon />}
      label={label}
      value={loadedForm[field] ?? ''}
      editValue={form[field] ?? ''}
      onChange={(value) => handleForm(field, type === 'number' && value !== '' ? Number(value) : value)}
      helperText={withOwnerNote(field, helperText)}
      type={type}
      showCurrentValue={!pendingRestart}
      disabled={locked(field)}
    />
  );
  const toggleField = (label: string, field: string, helperText = '') => (
    <ResponsiveSettingItem
      icon={<SettingsIcon />}
      label={label}
      value={loadedForm[field] ? 'Enabled' : 'Disabled'}
      editValue={form[field] ?? false}
      onChange={(value) => handleForm(field, value === 'true')}
      helperText={withOwnerNote(field, helperText)}
      type="select"
      options={[{ value: 'true', label: 'Enabled' }, { value: 'false', label: 'Disabled' }]}
      showCurrentValue={!pendingRestart}
      disabled={locked(field)}
    />
  );

  // A switch; `confirmOff` asks before turning it off (one sentence), never before turning it on.
  const switchField = (label: string, field: string, helperText: string,
    options: { inverted?: boolean; disabled?: boolean; confirmOff?: { title: string; text: string; action: string } } = {}) => {
    const value = Boolean(form[field]);
    const note = withOwnerNote(field, helperText);
    return (
      <Box sx={{ px: 2 }} data-testid={`switch-${field}`}>
        <FormControlLabel
          control={<Switch checked={options.inverted ? !value : value} disabled={options.disabled || locked(field)}
            onChange={(event) => {
              const next = options.inverted ? !event.target.checked : event.target.checked;
              if (!event.target.checked && options.confirmOff) {
                setConfirming({ field, value: next, ...options.confirmOff });
                return;
              }
              handleForm(field, next);
            }} />}
          label={label} />
        {note && <Typography variant="body2" color="text.secondary" sx={{ ml: 6 }}>{note}</Typography>}
      </Box>
    );
  };
  const readOnlyField = (label: string, field: string, value: string | undefined) => (
    <ResponsiveSettingItem key={field} icon={<StorageIcon />} label={label} value={value ?? ''}
      editValue={value || 'Not set'} showCurrentValue={false}
      helperText={field === 'faxbot_config_path' ? 'Set when Faxbot started. (FAXBOT_CONFIG_PATH)' : 'Read only. Changing it is a planned maintenance task.'} />
  );
  // Receiving goes through the receiving provider chosen in the Setup wizard; some providers only send.
  const receiver = String(effectiveInbound || '');
  const receiverName = receiver ? providerLabel(receiver) : '';
  const receiverCanReceive = !!receiver && (!BUILTIN_PROVIDERS.includes(receiver) || RECEIVING_PROVIDERS.has(receiver));
  const receivingSentence = !receiver
    ? 'No provider receives faxes yet. To receive, choose Add or change a provider.'
    : !receiverCanReceive
      // While receiving is on, the warning below says what to do.
      ? (form.inbound_enabled ? '' : `${receiverName} cannot receive faxes. To receive, choose Add or change a provider.`)
      : form.inbound_enabled
        ? `Faxes arrive through ${receiverName}.`
        : `Turn this on to receive faxes through ${receiverName}.`;

  const fetchSettings = async () => {
    if (actionFence.current) return;
    const epoch = ++requestEpoch.current;
    setNeedsReload(true);
    try {
      setError(null);
      setLoading(true);
      const data = await client.getSettings();
      if (epoch !== requestEpoch.current) return;
      if (!data._meta?.desired_revision_id) throw new Error('Settings could not be loaded. Select Reload to try again.');
      hydrate(data);
      try {
        const cfg = await client.getConfig();
        if (epoch === requestEpoch.current) {
          setDocsBase(cfg?.branding?.docs_base || DEFAULT_DOCS_BASE);
          setAllowRestart(cfg?.allow_restart === true);
        }
      } catch { /* Settings remain usable when branding is unavailable. */ }
      try {
        const message = await client.getFaxEngineMessage();
        if (epoch === requestEpoch.current) setEngineMessage(message);
      } catch { /* Readiness is optional here; Diagnostics shows it in full. */ }
    } catch (err) {
      if (epoch === requestEpoch.current) setError(accessErrorMessage(err));
    } finally {
      if (epoch === requestEpoch.current) setLoading(false);
    }
  };

  useEffect(() => {
    actionFence.current = false;
    setLoading(false);
    setSettings(null);
    setForm({});
    setLoadedForm({});
    setSaveResult(null);
    setSnack(null);
    setEnvContent('');
    setLastGeneratedSecret('');
    void fetchSettings();
    return () => { requestEpoch.current += 1; };
  }, [client]);

  useEffect(() => {
    if (!settings || !focus) return;
    if (focus === 'email') document.getElementById(EMAIL_DELIVERY_SECTION)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    if (focus === 'trunk') document.getElementById(SIP_TRUNK_SECTION)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    onFocused?.();
  }, [settings, focus, onFocused]);

  const exportEnv = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.exportSettings();
      setEnvContent(data.env);
    } catch (err) {
      setError(accessErrorMessage(err));  // one plain sentence, never "API Error: 500"
    } finally {
      setLoading(false);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  const downloadText = (filename: string, text: string) => {
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const getStatusIcon = (configured: boolean) => {
    return configured ? (
      <CheckCircleIcon color="success" />
    ) : (
      <ErrorIcon color="error" />
    );
  };

  // The fax engine's manager connection and the secret it sends with each received fax.
  const amiFields = () => settings && (
    <>
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.sip.ami_host)}
                      label="Fax engine address"
                      value={settings.sip.ami_host || ''}
                      editValue={form.ami_host ?? ''}
                      helperText="The fax engine's name on this server's private network, usually asterisk."
                      placeholder="For example, asterisk"
                      onChange={(value) => handleForm('ami_host', value)}
                      showCurrentValue={!pendingRestart && (!!settings.sip.ami_host)}
                    />
                    
                    {textField('Fax engine port', 'ami_port', '', 'number')}
                    {textField('Fax engine user name', 'ami_username')}
                    <ResponsiveSettingItem
                      icon={settings.sip.ami_password_is_default ? <WarningIcon color="warning" /> : <CheckCircleIcon color="success" />}
                      label="Fax engine password"
                      value={settings.sip.ami_password_is_default ? 'Still the default password (not safe)' : 'Own password set'}
                      editValue={form.ami_password ?? ''}
                      helperText="Must match the fax engine's own setting and must not be the default. Never open its port to the internet."
                      placeholder="Enter a new password"
                      onChange={(value) => handleForm('ami_password', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (!settings.sip.ami_password_is_default)}
                      {...envField('ami_password')}
                    />
    </>
  );
  const inboundSecret = () => settings && (
              <Box sx={{ mt: 2 }}>
                <ResponsiveSettingItem
                  icon={<SecurityIcon />}
                  label="Fax engine secret for received faxes"
                  value={lastGeneratedSecret ? 'New secret, shown below' : (settings.inbound.sip?.configured ? 'Configured' : 'Not configured')}
                  editValue={form.asterisk_inbound_secret ?? ''}
                  helperText="The fax engine sends this with each fax it receives, so Faxbot knows the fax is real. Keep it private."
                  onChange={(value) => handleForm('asterisk_inbound_secret', value)}
                  placeholder="Secret"
                  type="password"
                  showCurrentValue={false}
                  {...envField('asterisk_inbound_secret')}
                />
                <Box sx={{ display: 'flex', gap: 1, mt: 1, flexWrap: 'wrap' }}>
                  <Button 
                    size="small" 
                    variant="outlined"
                    onClick={async () => {
                      try {
                        const bytes = new Uint8Array(32);
                        const cryptoObj: any = (typeof window !== 'undefined') ? (window as any).crypto : undefined;
                        if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
                          cryptoObj.getRandomValues(bytes);
                        } else {
                          throw new Error('This browser can’t generate a secure secret.');
                        }
                        const b64 = btoa(String.fromCharCode(...Array.from(bytes))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
                        setLastGeneratedSecret(b64);
                        handleForm('asterisk_inbound_secret', b64);
                        setSnack('New secret generated. Apply settings to save it.');
                      } catch(e:any){ setError(e?.message||'A new secret could not be made.'); }
                    }}
                    sx={{ borderRadius: 1 }}
                  >
                    Make a new secret
                  </Button>
                  <Button 
                    size="small" 
                    variant="outlined"
                    onClick={async () => {
                      const toCopy = String(form.asterisk_inbound_secret || lastGeneratedSecret || '').trim();
                      if (!toCopy || toCopy === loadedForm.asterisk_inbound_secret) return;
                      try { await navigator.clipboard.writeText(toCopy); setSnack('Copied'); } catch {}
                    }} 
                    disabled={!form.asterisk_inbound_secret || form.asterisk_inbound_secret === loadedForm.asterisk_inbound_secret}
                    sx={{ borderRadius: 1 }}
                  >
                    Copy
                  </Button>
                </Box>
                {lastGeneratedSecret && (
                  <Alert severity="success" sx={{ mt: 2, borderRadius: 2 }}>
                    <Typography variant="body2">
                      New secret (copy now): <code>{lastGeneratedSecret}</code>
                    </Typography>
                  </Alert>
                )}
              </Box>
  );

  return (
    <Box>
      <Box display="flex" justifyContent={sections && !title ? 'flex-end' : 'space-between'} alignItems="center" mb={3}>
        {(!sections || title) && (
          <Typography variant="h4" component="h1">
            {sections ? title : 'Settings'}
          </Typography>
        )}
        <Box>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchSettings}
            disabled={loading}
            sx={{ mr: 1 }}
          >
            Reload
          </Button>
          {shows('backup') && (<>
          <Button variant="contained" onClick={exportEnv} disabled={loading} sx={{ mr: 1 }}>
            Export settings
          </Button>
          <Button
            variant="outlined"
            onClick={async () => {
              try {
                setLoading(true); setError(null);
                const res = await client.persistSettings();
                setSnack(`Recovery copy saved to ${res.path}. A full backup also needs the database and the installation key.`);
              } catch (e: any) {
                setError(e?.message || 'The recovery copy could not be saved.');
              } finally { setLoading(false); }
            }}
            disabled={loading}
          >
            Save a recovery copy
          </Button>
          <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }} data-testid="recovery-retiring">
            The recovery copy goes away in the next release. Make a full backup on the server instead (faxbot system backup).
          </Typography>
          </>)}

        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}
      {engineMessage && (shows('providers') || shows('trunk')) && (
        <Alert severity="error" sx={{ mb: 3 }} data-testid="engine-message">
          {engineMessage}
        </Alert>
      )}
      {settings && needsReload && !loading && !error ? (
        <Alert severity="warning" sx={{ mb: 3 }}>
          Editing is paused. Select Reload to continue.
        </Alert>
      ) : settings && pendingRestart && !needsReload ? (
        <Box sx={{ mb: 3 }}>
          <RestartNotice client={client} text={restartMessage} canRestart={canRestart && allowRestart}
            onBack={async () => { await fetchSettings(); setRestarted(true); }} />
        </Box>
      ) : settings && restarted && !needsReload ? (
        <Alert severity="success" sx={{ mb: 3 }} onClose={() => setRestarted(false)}>
          Faxbot restarted and is using the saved settings.
        </Alert>
      ) : null}

      {loading && !settings ? (
        <Box display="flex" justifyContent="center" py={4}>
          <CircularProgress />
        </Box>
      ) : settings ? (
        <Box>
        <Box component="fieldset" disabled={!canEdit} sx={{ border: 0, m: 0, p: 0, minWidth: 0 }}>
        <Stack spacing={3}>
          {/* Fax providers: the same two choices as the Setup Wizard */}
          {shows('providers') && (
          <ResponsiveFormSection
            title="Fax providers"
            subtitle="Which provider sends your faxes and which receives them"
            icon={<CloudIcon />}
          >
            {/* On the console's In use page the providers are listed above, and changed in the Setup wizard. */}
            {!sections && (
            <Box sx={{ px: 2 }} data-testid="provider-directions">
              <ProviderDirectionFields value={providerChoice} disabled={!canEdit} saved={loadedDirections(settings)}
                onChange={(next) => {
                  if (!canEdit || actionFence.current) return;
                  setForm((prev) => ({ ...prev, ...directionFields(next, settings) }));
                }} />
              {!pendingRestart && (
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  {`In use: ${directionSummary(loadedDirections(settings).sending, loadedDirections(settings).receiving)}`}
                </Typography>
              )}
            </Box>
            )}
            {!effectiveOutbound && (
              <Typography variant="body2" sx={{ px: 2 }} data-testid="no-provider">
                No fax provider set up yet.{' '}
                <Link href={docsLink('providers', docsBase)} target="_blank" rel="noreferrer">Provider setup</Link>
              </Typography>
            )}
            {effectiveOutbound === 'humblefax' && (
              <Typography variant="body2" sx={{ px: 2 }} data-testid="humblefax-countries">
                HumbleFax sends only to US and Canadian numbers.
              </Typography>
            )}

            {switchField('Sending is on', 'fax_disabled',
              'Off: Faxbot stops sending. Faxes submitted while sending is off stay on hold after you turn it back on.',
              { inverted: true, confirmOff: { title: 'Turn off sending?', action: 'Turn off sending',
                text: 'Faxbot will stop sending, and faxes submitted while sending is off stay on hold until you turn it back on.' } })}
            {settings.numbers && (
              <ResponsiveSettingItem
                icon={<PublicIcon />}
                label="Installation country"
                value={countryName(String(loadedForm.fax_default_country ?? settings.numbers.default_country))}
                helperText={COUNTRY_HELP}
                showCurrentValue={!pendingRestart}
                renderControl={({ id, labelledBy, describedBy }) => (
                  <CountryField id={id} labelledBy={labelledBy} describedBy={describedBy} size="small"
                    value={String(form.fax_default_country ?? settings.numbers!.default_country)}
                    countries={settings.numbers!.supported_countries}
                    disabled={!canEdit}
                    onChange={(code) => handleForm('fax_default_country', code)} />
                )}
              />
            )}
          </ResponsiveFormSection>
          )}

          {/* Security Settings */}
          {shows('security') && (
          <ResponsiveFormSection
            title="Security"
            subtitle="How people sign in and how this server is reached."
            icon={<SecurityIcon />}
          >
            <ResponsiveSettingItem
              icon={<CheckCircleIcon color="success" />}
              label="Authentication"
              editValue="Required"
              helperText="Every request needs a signed-in person or an API key; manage them in Keys and Users."
              showCurrentValue={false}
            />
            
            <ResponsiveSettingItem
              icon={settings.security.enforce_https ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
              label="Require HTTPS for document links"
              value={settings.security.enforce_https ? 'Yes' : 'No'}
              editValue={form.enforce_public_https ?? settings.security.enforce_https}
              helperText={withOwnerNote('enforce_public_https', "Links to fax documents work only over a secure connection.")}
              onChange={(value) => handleForm('enforce_public_https', value === 'true')}
              type="select"
              options={[
                { value: 'true', label: 'Yes' },
                { value: 'false', label: 'No' }
              ]}
              showCurrentValue={!pendingRestart}
              disabled={locked('enforce_public_https')}
            />
            
            
            {/* enable_persisted_settings is read from the environment when a new installation first starts. */}
            <ResponsiveSettingItem
              icon={<StorageIcon />}
              label="Restore from the recovery copy on a fresh start"
              editValue={settings.persisted?.enabled ? 'On' : 'Off'}
              helperText={`${ENV_IMPORT_HELP} This setting goes away in the next release.`}
              showCurrentValue={false}
            />
            {textField("This server's public address", 'public_api_url', 'The address people and fax services use to reach Faxbot, such as https://fax.example.com.')}
            <DeploymentRows settings={settings}
              names={['FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS', 'FAXBOT_CONSOLE_ORIGINS', 'ENABLE_LOCAL_ADMIN']} />
          </ResponsiveFormSection>
          )}

          {/* System > Audit log: the event record Logs reads */}
          {shows('audit') && (
          <ResponsiveFormSection title="Event recording" icon={<SettingsIcon />}
            subtitle="Faxbot can record what happens, such as sign-ins and sent faxes, for the Logs page. Changes take effect after Faxbot restarts.">
            {switchField('Record events', 'audit_log_enabled', 'Sign-ins, setting changes and fax activity, shown in Logs.')}
            <ResponsiveSettingItem
              icon={<SettingsIcon />}
              label="How each event is written"
              value={loadedForm.audit_log_format === 'text' ? 'Plain text' : 'Structured'}
              editValue={form.audit_log_format ?? 'json'}
              helperText={withOwnerNote('audit_log_format', 'Structured is easier for other programs to read.')}
              onChange={(value) => handleForm('audit_log_format', value)}
              type="select"
              options={[{ value: 'json', label: 'Structured' }, { value: 'text', label: 'Plain text' }]}
              showCurrentValue={!pendingRestart}
              disabled={locked('audit_log_format')}
            />
            {textField('Also save events in a file on the server', 'audit_log_file', 'Leave empty to keep them only in Logs.')}
            {switchField('Also send events to the system log', 'audit_log_syslog', 'For servers that collect logs in one place.')}
            {textField('Address of the system log', 'audit_log_syslog_address', "Leave this as it is unless the server's log collector uses another address.")}
          </ResponsiveFormSection>
          )}

          {/* Backend-Specific Configuration */}
          {providerShown('phaxio', 'phaxio') && (
                  <ResponsiveSettingSection
                    title={providerTitle('Phaxio')}
                    subtitle="Your Phaxio account"
                  >
                    {providerStatus('phaxio')}
                    <Box sx={{ display: 'flex', gap: 1, mb: 2, flexWrap: 'wrap' }}>
                      <Chip
                        label="Phaxio setup guide"
                        component="a"
                        href={docsLink('phaxio', docsBase)}
                        target="_blank"
                        rel="noreferrer"
                        clickable
                        size="small"
                        variant="outlined"
                      />
                    </Box>
                    
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.phaxio.api_key)}
                      label="API Key"
                      value={settings.phaxio.api_key?.replace(/./g, '*').slice(0, 20) || ''}
                      editValue={form.phaxio_api_key ?? ''}
                      helperText="Copy it from your Phaxio account and keep it private."
                      placeholder="Enter a new API key"
                      onChange={(value) => handleForm('phaxio_api_key', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (!!settings.phaxio.api_key)}
                      {...envField('phaxio_api_key')}
                    />
                    
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.phaxio.api_secret)}
                      label="API Secret"
                      value={settings.phaxio.api_secret?.replace(/./g, '*').slice(0, 20) || ''}
                      editValue={form.phaxio_api_secret ?? ''}
                      helperText="Shown next to the API key in your Phaxio account."
                      placeholder="Enter a new API secret"
                      onChange={(value) => handleForm('phaxio_api_secret', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (!!settings.phaxio.api_secret)}
                      {...envField('phaxio_api_secret')}
                    />
                    
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.phaxio.callback_token)}
                      label="Callback Token"
                      value={settings.phaxio.callback_token ?? ''}
                      editValue={form.phaxio_callback_token ?? ''}
                      helperText="From your Phaxio account (not the API secret). Faxbot uses it to check that status updates come from Phaxio."
                      placeholder="Enter a new callback token"
                      onChange={(value) => handleForm('phaxio_callback_token', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && !!settings.phaxio.callback_token}
                      {...envField('phaxio_callback_token')}
                    />

                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.phaxio.callback_url)}
                      label="Address for Phaxio status updates (optional)"
                      value={settings.phaxio.callback_url ?? ''}
                      editValue={form.phaxio_status_callback_url ?? ''}
                      helperText="Leave it empty: Faxbot uses its own public address."
                      placeholder="https://localhost:8080/phaxio-callback"
                      onChange={(value) => handleForm('phaxio_status_callback_url', value)}
                      showCurrentValue={!pendingRestart && (!!settings.phaxio.callback_url)}
                    />
                    {toggleField('Check that status updates come from Phaxio', 'phaxio_verify_signature', "When off, Faxbot ignores Phaxio's status updates and asks Phaxio about each fax instead.")}
                  </ResponsiveSettingSection>
                )}

                {providerShown('sinch', 'sinch') && (
                  <ResponsiveSettingSection title={providerTitle('Sinch')} subtitle="Your Sinch account">
                    {providerStatus('sinch')}
                    {textField('Sinch Project ID', 'sinch_project_id')}
                    {textField('Sinch address (optional)', 'sinch_base_url', 'Leave it empty to use the usual Sinch address.')}
                    {textField('Sinch API Key', 'sinch_api_key', 'Leave unchanged to keep the saved key.', 'password')}
                    {textField('Sinch API Secret', 'sinch_api_secret', 'Leave unchanged to keep the saved secret.', 'password')}
                    {textField('Address Sinch sends received faxes to (optional)', 'sinch_webhook_base_url',
                      "Leave it empty to use this server's public address. Fill it in when Sinch reaches Faxbot at another address, such as https://fax-hooks.example.com.")}
                    {settings.sinch?.incoming_webhook_url && (
                      <Alert severity="info" sx={{ mt: 2 }} data-testid="sinch-incoming-webhook">
                        <Typography variant="body2" sx={{ fontWeight: 600 }}>Incoming webhook URL</Typography>
                        <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                          {settings.sinch.incoming_webhook_login_url || settings.sinch.incoming_webhook_url}
                        </Typography>
                        <Typography variant="body2" sx={{ mt: 1 }}>
                          {settings.sinch.incoming_webhook_login_url
                            ? 'In the Sinch dashboard, open Fax, then Services, click Edit beside your fax service and paste this into Incoming webhook URL, with your password in place of PASSWORD. Sinch then shows the password as ***.'
                            : 'In the Sinch dashboard, open Fax, then Services, click Edit beside your fax service and paste this into Incoming webhook URL. Faxbot checks each fax with Sinch before it keeps it.'}
                        </Typography>
                      </Alert>
                    )}
                  </ResponsiveSettingSection>
                )}

                {providerShown('documo', 'documo') && (
                  <ResponsiveSettingSection
                    title={providerTitle('Documo')}
                    subtitle="Your Documo account"
                  >
                    {providerStatus('documo')}
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings?.documo?.configured)}
                      label="Documo API Key"
                      value={settings?.documo?.configured ? 'Configured' : ''}
                      editValue={form.documo_api_key ?? ''}
                      helperText="Copy it from your Documo account."
                      placeholder="Documo API key"
                      onChange={(value) => handleForm('documo_api_key', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (settings?.documo?.configured)}
                      {...envField('documo_api_key')}
                    />
                    
                    {textField('Documo address (optional)', 'documo_base_url', 'Leave it empty to use the usual Documo address.')}
                    <ResponsiveSettingItem
                      icon={getStatusIcon(true)}
                      label="Documo test mode"
                      value={settings.documo?.sandbox ? 'Test (sandbox)' : 'Real faxes'}
                      editValue={form.documo_use_sandbox ?? settings.documo?.sandbox ?? false}
                      helperText="Test mode sends nothing to real fax numbers."
                      onChange={(value) => handleForm('documo_use_sandbox', value === 'true')}
                      type="select"
                      options={[
                        { value: 'false', label: 'Real faxes' },
                        { value: 'true', label: 'Test (sandbox)' }
                      ]}
                      showCurrentValue={false}
                    />
                  </ResponsiveSettingSection>
                )}

                {providerShown('humblefax', 'humblefax') && (
                  <ResponsiveSettingSection
                    title={providerTitle('HumbleFax')}
                    subtitle="Your HumbleFax account"
                  >
                    {providerStatus('humblefax')}
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings?.humblefax?.configured)}
                      label="HumbleFax Access Key"
                      value={settings?.humblefax?.configured ? 'Configured' : ''}
                      editValue={form.humblefax_access_key ?? ''}
                      helperText="Enter the access key from your HumbleFax account."
                      placeholder="Access key"
                      onChange={(value) => handleForm('humblefax_access_key', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (settings?.humblefax?.configured)}
                      {...envField('humblefax_access_key')}
                    />
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings?.humblefax?.configured)}
                      label="HumbleFax Secret Key"
                      value={settings?.humblefax?.configured ? 'Configured' : ''}
                      editValue={form.humblefax_secret_key ?? ''}
                      helperText="Enter the secret key from your HumbleFax account."
                      placeholder="Secret key"
                      onChange={(value) => handleForm('humblefax_secret_key', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (settings?.humblefax?.configured)}
                      {...envField('humblefax_secret_key')}
                    />
                    {textField('Send from this HumbleFax number', 'humblefax_from_number', 'Optional. 10 digits, or 11 digits starting with 1. Leave empty to use the account default number.')}
                    <HumbleFaxReceiving values={form} onChange={handleForm} disabled={!canEdit} client={client}
                      receivingProvider={effectiveInbound === 'humblefax' && !!form.inbound_enabled} settings={settings} />
                  </ResponsiveSettingSection>
                )}

                {providerShown('efax', 'efax') && (
                  <ResponsiveSettingSection title={providerTitle('eFax')} subtitle="Your eFax Enterprise account">
                    {providerStatus('efax')}
                    <EfaxSettings values={form} onChange={handleForm} settings={settings} disabled={!canEdit}
                      receives={effectiveInbound === 'efax' && !!form.inbound_enabled} docsHref={docsLink('efax', docsBase)}
                      client={client} />
                  </ResponsiveSettingSection>
                )}

                {providerShown('sip', 'trunk') && (
                  <ResponsiveSettingSection
                    title={sections ? 'Settings' : 'Your fax line'}
                    subtitle={sections ? 'Your fax line, its numbers and how Faxbot connects to it.' : 'Your fax line and how Faxbot connects to its fax engine.'}
                  >
                    {providerStatus('sip')}
                    {!sections && amiFields()}
                    {/* On the console's own pages the station ID is under Numbers, Sender identity. */}
                    {!sections && (
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.sip.station_id)}
                      label="Station ID"
                      value={settings.sip.station_id || ''}
                      editValue={form.fax_station_id ?? ''}
                      helperText={internationalHint(settingsNumberFormat(settings), 'Your fax number')}
                      placeholder={settingsNumberFormat(settings)?.international || undefined}
                      onChange={(value) => handleForm('fax_station_id', value)}
                      showCurrentValue={!pendingRestart && (!!settings.sip.station_id)}
                    />
                    )}
                    <Box id={SIP_TRUNK_SECTION}><SipTrunkSettings client={client} presetChosenElsewhere={Boolean(sections)} /></Box>
                    {((settings.sip as { trunk?: { preset?: string } }).trunk?.preset === 'telnyx' || settings.sip.telnyx_api_key_set) && (
                      <Box sx={{ mt: 2 }} data-testid="telnyx-key">
                        <ResponsiveSettingItem
                          icon={getStatusIcon(!!settings.sip.telnyx_api_key_set)}
                          label="Telnyx API key"
                          value={settings.sip.telnyx_api_key_set ? 'Saved' : ''}
                          editValue={form.telnyx_api_key ?? ''}
                          helperText="Optional. With it, Faxbot shows what Telnyx charged for each call and checks fax over IP (T.38) and caller-name lookup on your numbers; it changes a number only when you select Turn on T.38 or Turn off name lookup."
                          placeholder="Telnyx API key"
                          onChange={(value) => handleForm('telnyx_api_key', value)}
                          type="password"
                          showCurrentValue={!pendingRestart && !!settings.sip.telnyx_api_key_set}
                          {...envField('telnyx_api_key')}
                        />
                      </Box>
                    )}
                    {sections && (
                      <Accordion disableGutters variant="outlined" sx={{ mt: 2 }} data-testid="fax-engine-connection">
                        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                          <Typography>Fax engine connection (advanced)</Typography>
                        </AccordionSummary>
                        <AccordionDetails>
                          <Typography variant="body2" sx={{ mb: 2 }}>
                            Faxbot sets this up itself. Change it only if you run your own fax engine.
                          </Typography>
                          {amiFields()}
                          {inboundSecret()}
                          <DeploymentRows settings={settings} names={['FAXBOT_MEDIA_PORTS', 'FAXBOT_PHONE_SYSTEM_ADDRESS']} />
                        </AccordionDetails>
                      </Accordion>
                    )}
                  </ResponsiveSettingSection>
                )}

          {/* Provider plugins (System > Developer) */}
          {shows('plugins') && (
          <ResponsiveFormSection
            title="Plugin settings"
            subtitle="Provider plugins installed on this server; changes take effect after a restart."
            icon={<SettingsIcon />}
          >
            {switchField('Use provider plugins', 'feature_v3_plugins',
              'Lets Faxbot send and receive through provider plugins installed on this server. Goes away in the next release.')}
            {switchField('Allow remote plugin installation (advanced)', 'feature_plugin_install',
              'Off by default for security. Turn on only in trusted environments.', { disabled: true })}
          </ResponsiveFormSection>
          )}

          {/* Inbound Receiving */}
          {shows('inbound') && (
          <ResponsiveFormSection
            title="Receiving"
            icon={<CheckCircleIcon />}
          >
            {switchField('Receiving is on', 'inbound_enabled', receivingSentence,
              { disabled: !form.inbound_enabled && !receiverCanReceive })}

            <ResponsiveSettingItem
              icon={<SettingsIcon />}
              label="Keep received faxes for (days)"
              value={String(settings.inbound?.retention_days ?? 30)}
              editValue={form.inbound_retention_days ?? settings.inbound?.retention_days ?? 30}
              helperText="After this many days Faxbot deletes a received fax's file. 0 keeps them."
              onChange={(value) => handleForm('inbound_retention_days', value === '' ? '' : Number(value))}
              type="number"
              placeholder={String(settings.inbound?.retention_days ?? 30)}
              showCurrentValue={!pendingRestart}
            />
            
            <ResponsiveSettingItem
              icon={<SettingsIcon />}
              label="Download links work for (minutes)"
              value={String(settings.inbound?.token_ttl_minutes ?? 60)}
              editValue={form.inbound_token_ttl_minutes ?? settings.inbound?.token_ttl_minutes ?? 60}
              helperText={withOwnerNote('inbound_token_ttl_minutes', "How long a link to download a received fax keeps working.")}
              onChange={(value) => handleForm('inbound_token_ttl_minutes', value === '' ? '' : Number(value))}
              type="number"
              placeholder={String(settings.inbound?.token_ttl_minutes ?? 60)}
              showCurrentValue={!pendingRestart}
              disabled={locked('inbound_token_ttl_minutes')}
            />

            {/* On the console's own pages this secret is in the trunk's fax engine connection box. */}
            {effectiveInbound === 'sip' && !sections && inboundSecret()}

            {effectiveInbound === 'phaxio' && (
              <ResponsiveSettingItem
                icon={settings.inbound?.phaxio?.verify_signature ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                label="Check that received faxes come from Phaxio"
                value={settings.inbound?.phaxio?.verify_signature ? 'Enabled' : 'Disabled'}
                editValue={form.phaxio_inbound_verify_signature ?? settings.inbound?.phaxio?.verify_signature ?? false}
                helperText={withOwnerNote('phaxio_inbound_verify_signature', "Recommended. Faxbot accepts a received fax only when Phaxio signed it.")}
                onChange={(value) => handleForm('phaxio_inbound_verify_signature', value === 'true')}
                type="select"
                options={[
                  { value: 'true', label: 'On (recommended)' },
                  { value: 'false', label: 'Disabled' }
                ]}
                showCurrentValue={!pendingRestart}
                disabled={locked('phaxio_inbound_verify_signature')}
              />
            )}

            {effectiveInbound === 'sinch' && (
              <Box sx={{ mt: 2 }}>
                <ResponsiveSettingItem
                  icon={settings.inbound?.sinch?.basic_auth_configured ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                  label="User name Sinch sends with received faxes (optional)"
                  value={settings.inbound?.sinch?.basic_auth_configured ? 'Configured' : 'Not configured'}
                  editValue={form.sinch_inbound_basic_user ?? ''}
                  helperText="Set the same user name and password in Sinch, and Faxbot requires them."
                  onChange={(value) => handleForm('sinch_inbound_basic_user', value)}
                  placeholder="User name"
                  showCurrentValue={false}
                />
                
                <ResponsiveSettingItem
                  icon={<SecurityIcon />}
                  label="Password Sinch sends with received faxes"
                  value=""
                  editValue={form.sinch_inbound_basic_pass ?? ''}
                  helperText="Set the same password in Sinch."
                  onChange={(value) => handleForm('sinch_inbound_basic_pass', value)}
                  placeholder="Password"
                  type="password"
                  showCurrentValue={false}
                  {...envField('sinch_inbound_basic_pass')}
                />
              </Box>
            )}
          </ResponsiveFormSection>
          )}

          <DeliverySettingsSections client={client} settings={settings} form={form} loaded={loadedForm}
            onChange={handleForm} showCurrentValue={!pendingRestart} outbound={String(effectiveOutbound)} canWrite={canWrite}
            only={sections?.filter((section): section is DeliverySection => DELIVERY_SECTIONS.includes(section as DeliverySection))} />

          {/* SignalWire (cloud) */}
          {providerShown('signalwire', 'signalwire') && (
            <ResponsiveFormSection
              title={providerTitle('SignalWire')}
              subtitle="Your SignalWire account"
              icon={<CloudIcon />}
            >
              {providerStatus('signalwire')}
              <ResponsiveSettingItem
                icon={<CloudIcon />}
                label="Space URL"
                value={settings.signalwire?.space_url || ''}
                editValue={form.signalwire_space_url ?? ''}
                helperText="Your SignalWire space address, such as example.signalwire.com."
                onChange={(value) => handleForm('signalwire_space_url', value)}
                placeholder="example.signalwire.com"
                showCurrentValue={!pendingRestart && (!!settings.signalwire?.space_url)}
              />
              
              <ResponsiveSettingItem
                icon={<SettingsIcon />}
                label="Project ID"
                value={settings.signalwire?.project_id || ''}
                editValue={form.signalwire_project_id ?? ''}
                helperText="Shown in your SignalWire account."
                onChange={(value) => handleForm('signalwire_project_id', value)}
                placeholder="Project ID"
                showCurrentValue={!pendingRestart && (!!settings.signalwire?.project_id)}
              />
              
              <ResponsiveSettingItem
                icon={<SecurityIcon />}
                label="API Token"
                value={settings.signalwire?.api_token ? '***' : ''}
                editValue={form.signalwire_api_token ?? ''}
                helperText="Copy it from your SignalWire account."
                onChange={(value) => handleForm('signalwire_api_token', value)}
                placeholder="API token"
                type="password"
                showCurrentValue={!pendingRestart && (!!settings.signalwire?.api_token)}
                {...envField('signalwire_api_token')}
              />
              
              <ResponsiveSettingItem
                icon={<SettingsIcon />}
                label="Send faxes from"
                value={settings.signalwire?.from_fax || ''}
                editValue={form.signalwire_fax_from_e164 ?? ''}
                helperText={internationalHint(settingsNumberFormat(settings), 'Your fax number')}
                onChange={(value) => handleForm('signalwire_fax_from_e164', value)}
                placeholder={settingsNumberFormat(settings)?.international || undefined}
                showCurrentValue={!pendingRestart && (!!settings.signalwire?.from_fax)}
              />
              {textField('Address for SignalWire status updates (optional)', 'signalwire_status_callback_url', 'Leave it empty: Faxbot uses its own public address.')}
              {textField('SignalWire signing key', 'signalwire_webhook_signing_key', 'Leave unchanged to keep the saved signing key.', 'password')}
              {textField('Send text messages from', 'signalwire_sms_from_e164', 'Leave unchanged to keep the saved number.')}
              {textField('Ask SignalWire for status every (seconds)', 'signalwire_status_poll_seconds', "0 stops asking; Faxbot then waits for SignalWire's updates.", 'number')}
            </ResponsiveFormSection>
          )}

          {/* Storage Configuration */}
          {shows('storage') && (
          <ResponsiveFormSection
            title="Where faxes are kept"
            subtitle="On this server, or in S3 storage (Amazon S3 or a compatible service)."
            icon={<StorageIcon />}
          >
            <ResponsiveSettingItem
              icon={getStatusIcon(settings.storage?.backend === 's3')}
              label="Keep fax files"
              value={settings.storage?.backend === 's3' ? 'In S3 storage' : 'On this server'}
              editValue={form.storage_backend ?? settings.storage?.backend ?? 'local'}
              helperText="Where fax files are stored: on this server, or in your S3 bucket."
              onChange={(value) => handleForm('storage_backend', value)}
              type="select"
              options={[
                { value: 'local', label: 'On this server' },
                { value: 's3', label: 'In S3 storage' }
              ]}
              showCurrentValue={!pendingRestart}
            />
            
            {(form.storage_backend ?? settings.storage?.backend) === 's3' && (
              <Box sx={{ mt: 2 }}>
                <ResponsiveSettingItem
                  icon={<StorageIcon />}
                  label="S3 Bucket"
                  value={settings.storage?.s3_bucket || ''}
                  editValue={form.s3_bucket ?? ''}
                  helperText="The bucket's name in your AWS account."
                  onChange={(value) => handleForm('s3_bucket', value)}
                  placeholder="my-fax-bucket"
                  showCurrentValue={!pendingRestart && (!!settings.storage?.s3_bucket)}
                />
                
                <ResponsiveSettingItem
                  icon={<CloudIcon />}
                  label="AWS region"
                  value={settings.storage?.s3_region || ''}
                  editValue={form.s3_region ?? ''}
                  helperText="Where the bucket is, such as us-east-1."
                  onChange={(value) => handleForm('s3_region', value)}
                  placeholder="us-east-1"
                  showCurrentValue={!pendingRestart && (!!settings.storage?.s3_region)}
                />
                
                <ResponsiveSettingItem
                  icon={<SettingsIcon />}
                  label="Folder in the bucket (optional)"
                  value={settings.storage?.s3_prefix || ''}
                  editValue={form.s3_prefix ?? ''}
                  helperText="Faxbot keeps its files under this folder name."
                  onChange={(value) => handleForm('s3_prefix', value)}
                  placeholder="faxes/"
                  showCurrentValue={!pendingRestart && (!!settings.storage?.s3_prefix)}
                />
                
                <ResponsiveSettingItem
                  icon={<CloudIcon />}
                  label="Storage address (optional)"
                  value={settings.storage?.s3_endpoint_url || ''}
                  editValue={form.s3_endpoint_url ?? ''}
                  helperText="Only for S3-compatible storage other than Amazon, such as MinIO."
                  onChange={(value) => handleForm('s3_endpoint_url', value)}
                  placeholder="Only for S3-compatible storage"
                  showCurrentValue={!pendingRestart && (!!settings.storage?.s3_endpoint_url)}
                />
                
                <ResponsiveSettingItem
                  icon={<SecurityIcon />}
                  label="Encryption key (optional)"
                  value={settings.storage?.s3_kms_enabled ? 'Configured' : 'Not set'}
                  editValue={form.s3_kms_key_id ?? ''}
                  helperText="An AWS KMS key that encrypts each file. Recommended for health information."
                  onChange={(value) => handleForm('s3_kms_key_id', value)}
                  placeholder="KMS key ID"
                  showCurrentValue={false}
                />
                
                <Box sx={{ mt: 2 }}>
                  <Button 
                    variant="outlined" 
                    onClick={async () => { 
                      try {
                        setLoading(true);
                        // The diagnostics report checks the saved bucket (read-only) when its check is turned on.
                        const report = await client.checkDiagnosticsNow();
                        const storage = report.sections.flatMap((section) => section.checks)
                          .find((check) => check.id === 'server.storage');
                        if (!storage) {
                          setSnack('Save your storage settings first, then check again.');
                        } else if (storage.status === 'attention') {
                          setSnack('Turn on Also check the S3 bucket under System → Diagnostics, then check again.');
                        } else {
                          setSnack(storage.sentence);
                        }
                      } catch(e: any) {
                        setError(e?.message || 'The bucket could not be checked.'); 
                      } finally { 
                        setLoading(false); 
                      } 
                    }}
                    sx={{ borderRadius: 2 }}
                  >
                    Check the bucket
                  </Button>
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 2, display: 'block', mt: 1 }}>
                    A full check needs Also check the S3 bucket turned on under System → Diagnostics, and AWS access for this server.
                  </Typography>
                </Box>
              </Box>
            )}
            <DeploymentRows settings={settings} names={['FAXBOT_INSTALLATION_KEY_PATH', 'FAXBOT_DIRECT_KEY_PATH']} />
          </ResponsiveFormSection>
          )}

          {/* Sender identity: on its own page under Numbers. */}
          {sections?.includes('identity') && (
          <ResponsiveFormSection title="Sender identity" icon={<SettingsIcon />}
            subtitle="What the receiving fax machine shows for faxes Faxbot sends over your carrier trunk or phone system.">
            {textField('Header text', 'fax_header', 'Printed at the top of each page, usually your organization name.')}
            <ResponsiveSettingItem
              icon={<SettingsIcon />}
              label="Station ID"
              value={settings.sip.station_id || ''}
              editValue={form.fax_station_id ?? ''}
              helperText={internationalHint(settingsNumberFormat(settings), 'Your fax number, shown to the receiving fax machine')}
              placeholder={settingsNumberFormat(settings)?.international || undefined}
              onChange={(value) => handleForm('fax_station_id', value)}
              showCurrentValue={!pendingRestart && (!!settings.sip.station_id)}
            />
            <Typography variant="body2" color="text.secondary" sx={{ px: 2 }}>
              Faxes sent through a fax service such as Phaxio or HumbleFax show the name and number set in that service's account.
            </Typography>
          </ResponsiveFormSection>
          )}

          {shows('mcp') && (
          <ResponsiveFormSection title="MCP Configuration" subtitle="Connections for AI assistants; changes take effect after a restart." icon={<SettingsIcon />}>
            {toggleField('Enable MCP SSE', 'enable_mcp_sse')}
            {textField('MCP SSE Path', 'mcp_sse_path')}
            {toggleField('Enable MCP HTTP', 'enable_mcp_http')}
            {textField('MCP HTTP Path', 'mcp_http_path')}
            {toggleField('Require MCP OAuth', 'require_mcp_oauth')}
            {textField('OAuth Issuer', 'oauth_issuer')}
            {textField('OAuth Audience', 'oauth_audience')}
            {textField('OAuth JWKS URL', 'oauth_jwks_url')}
            <DeploymentRows settings={settings} showNames names={['MCP_ALLOWED_HOSTS', 'MCP_ALLOWED_ORIGINS',
              'MCP_OAUTH_SUBJECT_KEYS_FILE', 'MCP_RESOURCE_URL', 'MCP_HTTP_PORT']} />
          </ResponsiveFormSection>
          )}

          {/* Advanced Settings */}
          {shows('advanced') && (
          <ResponsiveFormSection
              title="Limits and cleanup"
              subtitle="The database, how much each key may ask for, and how long files are kept."
              icon={<SettingsIcon />}
            >
              <Stack spacing={3}>
                <Box>
                  <ResponsiveSettingItem
                    icon={<StorageIcon />}
                    label="Database (read only)"
                    value={settings.database?.url ?? ''}
                    editValue={settings.database?.url ?? ''}
                    helperText="Passwords in it are hidden."
                    showCurrentValue={false}
                  />
                  <Alert severity="info" sx={{ mt: 2 }}>
                    The database can’t be changed here; moving to a new database is a planned maintenance task.
                  </Alert>
                </Box>

                {/* Upload Limits */}
                <ResponsiveTextField
                  label="Largest document (MB)"
                  value={String(form.max_file_size_mb ?? settings.limits?.max_file_size_mb ?? 10)}
                  onChange={(value) => handleForm('max_file_size_mb', value === '' ? '' : Number(value))}
                  placeholder="10"
                  helperText="Largest document Faxbot accepts; your provider may have its own limit."
                  type="number"
                  icon={<CloudIcon />}
                />

                {/* Rate Limiting */}
                <ResponsiveTextField
                  label="Requests per minute for each key"
                  value={String(form.max_requests_per_minute ?? settings.limits?.rate_limit_rpm ?? 60)}
                  onChange={(value) => handleForm('max_requests_per_minute', value === '' ? '' : Number(value))}
                  placeholder="60"
                  helperText={withOwnerNote('max_requests_per_minute', "How many requests each key may make per minute. 0 turns the limit off.")}
                  type="number"
                  icon={<SecurityIcon />}
                  disabled={locked('max_requests_per_minute')}
                />

                <ResponsiveTextField
                  label="Received-fax lists per minute"
                  value={String(form.inbound_list_rpm ?? settings.limits?.inbound_list_rpm ?? 30)}
                  onChange={(value) => handleForm('inbound_list_rpm', value === '' ? '' : Number(value))}
                  placeholder="30"
                  helperText={withOwnerNote('inbound_list_rpm', "How often each key may list received faxes, per minute.")}
                  type="number"
                  icon={<SecurityIcon />}
                  disabled={locked('inbound_list_rpm')}
                />

                <ResponsiveTextField
                  label="Received-fax downloads per minute"
                  value={String(form.inbound_get_rpm ?? settings.limits?.inbound_get_rpm ?? 60)}
                  onChange={(value) => handleForm('inbound_get_rpm', value === '' ? '' : Number(value))}
                  placeholder="60"
                  helperText={withOwnerNote('inbound_get_rpm', "How often each key may open or download a received fax, per minute.")}
                  type="number"
                  icon={<SecurityIcon />}
                  disabled={locked('inbound_get_rpm')}
                />

                {textField('Document links for fax services work for (minutes)', 'pdf_token_ttl_minutes', 'How long a fax service may fetch a document Faxbot sends through it.', 'number')}
                {textField('Keep sent fax files for (days)', 'artifact_ttl_days', '0 keeps them.', 'number')}
                {textField('Clean up old files every (minutes)', 'cleanup_interval_minutes', '', 'number')}
                <Alert 
                  severity="info" 
                  sx={{ 
                    borderRadius: 2,
                    '& .MuiAlert-icon': { alignItems: 'center' }
                  }}
                >
                  <Typography variant="body2">
                    For health information, keep these limits and the largest document size within your policy.
                  </Typography>
                </Alert>
              </Stack>
            </ResponsiveFormSection>
          )}

          {/* System > Diagnostics */}
          {shows('diagnostics') && (
          <ResponsiveFormSection title="Diagnostics options" icon={<SettingsIcon />}>
            {[
              ['enable_s3_diagnostics', 'Also check the S3 bucket',
                'When on, Diagnostics also make sure Faxbot can reach the online storage that holds your faxes.'],
              ['admin_allow_restart', 'Allow restarting Faxbot from here',
                'Turn this on only if Faxbot starts again by itself after it stops. Docker Compose installs do; elsewhere, check your service manager.'],
            ].map(([field, label, help]) => (
              <Box key={field} sx={{ mb: 2 }}>
                {switchField(label, field, help)}
                <Box sx={{ px: 2, mt: 1 }}>
                  <Button size="small" variant="outlined" onClick={() => void applySettings([field])}
                    disabled={!canEdit || !changedFields.includes(field)} aria-label={`Apply: ${label}`}>
                    Apply
                  </Button>
                </Box>
              </Box>
            ))}
            <DeploymentRows settings={settings} names={['TZ']} />
          </ResponsiveFormSection>
          )}

          {/* Access > Keys & phones: the installation key (api_key), shown read-only and never by value */}
          {sections?.includes('installation-key') && (
          <ResponsiveFormSection title="Installation key" icon={<SecurityIcon />}>
            <ResponsiveSettingItem icon={<SecurityIcon />} label="Installation key"
              editValue={settings.security.api_key ? 'Set in .env' : 'Not set'} showCurrentValue={false}
              helperText="Set when Faxbot was first installed. Faxbot never shows it; only the owner can replace it." />
          </ResponsiveFormSection>
          )}

          {/* Access > Keys & phones */}
          {shows('phones') && (
          <ResponsiveFormSection title="Phones on your network" icon={<PublicIcon />}>
            {textField('Address phones use on your network', 'mobile_local_base',
              "Enter this computer's address on your office network, for phones there to use. Leave it empty if phones connect only over the internet.")}
          </ResponsiveFormSection>
          )}

          {/* System > Developer */}
          {shows('developer') && (
          <ResponsiveFormSection title="Developer settings" subtitle="Help links and the files Faxbot reads." icon={<SettingsIcon />}>
            {textField('Documentation address', 'docs_base_url', 'Where help links in the console point.')}
            {READ_ONLY_FILES.map(({ field, label, value }) => readOnlyField(label, field, value(settings)))}
            <DeploymentRows settings={settings} showNames names={['FAXBOT_ALLOW_INSECURE_LOOPBACK']} />
          </ResponsiveFormSection>
          )}
        </Stack>
        </Box>
        <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
          {!(sections && sections.every((section) => OWN_APPLY_SECTIONS.has(section))) && (
          <Button variant="contained" onClick={() => void applySettings()} disabled={!canEdit || changedFields.length === 0}>
            Apply settings
          </Button>
          )}
        </Box>

        </Box>
      ) : (
        <Typography variant="body2" color="text.secondary">
          Select Reload to view and edit settings.
        </Typography>
      )}

      <Dialog open={confirming !== null} onClose={() => setConfirming(null)} maxWidth="xs" fullWidth>
        <DialogTitle>{confirming?.title}</DialogTitle>
        <DialogContent><Typography variant="body2">{confirming?.text}</Typography></DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirming(null)}>Cancel</Button>
          <Button variant="contained" color="warning" onClick={() => {
            if (confirming) handleForm(confirming.field, confirming.value);
            setConfirming(null);
          }}>{confirming?.action}</Button>
        </DialogActions>
      </Dialog>

      {envContent && shows('backup') && (
        <Card sx={{ mt: 3 }}>
          <CardContent>
            <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
              <Typography variant="h6">
                Redacted .env export
              </Typography>
              <Box>
                <Button
                  variant="outlined"
                  startIcon={<ContentCopyIcon />}
                  onClick={() => copyToClipboard(envContent)}
                  sx={{ mr: 1 }}
                >
                  Copy
                </Button>
                <Button
                  variant="outlined"
                  startIcon={<DownloadIcon />}
                  onClick={() => downloadText('faxbot.redacted.env', envContent)}
                >
                  Download
                </Button>
              </Box>
            </Box>
            
            <Paper sx={{ p: 2, bgcolor: 'background.default' }}>
              <pre style={{ 
                margin: 0, 
                fontSize: '0.875rem', 
                overflow: 'auto',
                whiteSpace: 'pre-wrap'
              }}>
                {envContent}
              </pre>
            </Paper>
            
            <Alert severity="warning" sx={{ mt: 2 }}>
              Secrets are masked; a full backup also needs the database and installation key.
            </Alert>
          </CardContent>
        </Card>
      )}
      {snack && (
        <Alert severity={saveResult?._meta.apply_state === 'pending_restart' ? 'warning' : 'success'} sx={{ mt: 2 }} onClose={() => setSnack(null)}>
          {snack}
        </Alert>
      )}
    </Box>
  );
}

export default Settings;
