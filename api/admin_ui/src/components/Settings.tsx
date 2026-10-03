import { useEffect, useRef, useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Button,
  Alert,
  Paper,
  CircularProgress,
  Grid,
  Chip,
  Switch,
  FormControlLabel,
  Stack,
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
} from '@mui/icons-material';
import AdminAPIClient, { configurationWriteRejected, isForbidden } from '../api/client';
import { DeliverySettingsSections, EMAIL_DELIVERY_SECTION, deliveryEditorValues } from './delivery/DeliverySettings';
import { DEFAULT_DOCS_BASE, docsLink } from '../docsLinks';
import type { ConfigurationWriteResult, Settings as SettingsType, SettingsPatch } from '../api/types';
import { ResponsiveSettingItem, ResponsiveSettingSection } from './common/ResponsiveSettingItem';
import { ResponsiveTextField, ResponsiveFormSection } from './common/ResponsiveFormFields';
import TunnelSettings from './TunnelSettings';
import SipTrunkSettings from './SipTrunkSettings';

interface SettingsProps {
  client: AdminAPIClient;
  // May this account change settings (email delivery actions are shown only then).
  canWrite?: boolean;
  // A section to scroll to once settings load, such as the email delivery settings.
  focus?: string | null;
  onFocused?: () => void;
}

type FormValue = string | number | boolean;
type SettingsForm = Record<string, FormValue>;

// Limits checked before saving, so a value the server would refuse gets a plain sentence.
const FIELD_RANGES: Record<string, { min: number; max: number; message: string }> = {
  route_min_success_percent: { min: 0, max: 100, message: 'Enter a minimum delivery rate from 0 to 100.' },
  intake_smtp_port: { min: 1, max: 65535, message: 'Enter an email server port from 1 to 65535.' },
};

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
    sinch_api_key: data.sinch.api_key,
    sinch_api_secret: data.sinch.api_secret,
    documo_api_key: data.documo?.api_key ?? '',
    documo_base_url: data.documo?.base_url ?? '',
    documo_use_sandbox: data.documo?.sandbox ?? false,
    humblefax_access_key: data.humblefax?.access_key ?? '',
    humblefax_secret_key: data.humblefax?.secret_key ?? '',
    humblefax_from_number: data.humblefax?.from_number ?? '',
    ami_host: data.sip.ami_host,
    ami_port: data.sip.ami_port,
    ami_username: data.sip.ami_username,
    ami_password: data.sip.ami_password,
    fax_station_id: data.sip.station_id,
    fs_esl_host: data.fs?.esl_host ?? '',
    fs_esl_port: data.fs?.esl_port ?? 8021,
    fs_esl_password: data.fs?.esl_password ?? '',
    fs_gateway_name: data.fs?.gateway_name ?? '',
    fs_caller_id_number: data.fs?.caller_id_number ?? '',
    fs_t38_enable: data.fs?.t38_enable ?? true,
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
    sinch_inbound_verify_signature: data.inbound.sinch?.verify_signature ?? true,
    sinch_inbound_basic_user: data.inbound.sinch?.basic_user ?? '',
    sinch_inbound_basic_pass: data.inbound.sinch?.basic_pass ?? '',
    sinch_inbound_hmac_secret: data.inbound.sinch?.hmac_secret ?? '',
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
    ...deliveryEditorValues(data),
  };
}

function Settings({ client, canWrite = false, focus, onFocused }: SettingsProps) {
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
  const effectiveInbound = form.inbound_backend || form.backend || loadedInbound;
  const providerSelected = (provider: string) => effectiveOutbound === provider || effectiveInbound === provider;
  const changedFields = Object.keys(form).filter((field) => form[field] !== loadedForm[field]);

  const hydrate = (data: SettingsType) => {
    const values = editorValues(data);
    setSettings(data);
    setForm(values);
    setLoadedForm(values);
    setLastGeneratedSecret('');
    setNeedsReload(false);
  };

  const applySettings = async () => {
    if (actionFence.current || loading) return;
    if (!desiredRevision) {
      setError('Load Settings before applying changes.');
      return;
    }
    actionFence.current = true;
    const epoch = requestEpoch.current;
    let writeStarted = false;
    try {
      setLoading(true);
      setError(null);
      setSnack(null);
      const patch: SettingsPatch = { expected_revision_id: desiredRevision };
      for (const field of changedFields) {
        const value = form[field];
        if (typeof loadedForm[field] === 'number' && (value === '' || !Number.isFinite(Number(value)) || !Number.isSafeInteger(Number(value)))) {
          throw new Error(`Enter a whole number for ${field.replace(/_/g, ' ')}. An empty number does not clear the setting.`);
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
      } catch {
        if (epoch !== requestEpoch.current) return;
        setError('The page could not refresh. Click Load Settings before making more changes.');
      }
    } catch (err) {
      if (epoch !== requestEpoch.current) return;
      const message = err instanceof Error ? err.message : 'Failed to apply settings';
      if (!writeStarted) {
        setError(message);
        return;
      }
      setNeedsReload(true);
      setError(message.includes('409')
        ? 'Someone else changed these settings. Your edits are kept here; reload to see the current values.'
        : isForbidden(err)
          ? 'You do not have permission to change some of these settings. Your edits are kept here; reload before trying again.'
        : configurationWriteRejected(err)
          ? `Settings were not saved (${message}). Your edits are kept here; reload before trying again.`
          : 'The save could not be confirmed. Reload to check whether your changes were saved.');
    } finally {
      if (epoch === requestEpoch.current) {
        actionFence.current = false;
        setLoading(false);
      }
    }
  };

  const textField = (label: string, field: string, helperText = '', type: 'text' | 'password' | 'number' = 'text') => (
    <ResponsiveSettingItem
      icon={type === 'password' ? <SecurityIcon /> : <SettingsIcon />}
      label={label}
      value={loadedForm[field] ?? ''}
      editValue={form[field] ?? ''}
      onChange={(value) => handleForm(field, type === 'number' && value !== '' ? Number(value) : value)}
      helperText={helperText}
      type={type}
      showCurrentValue={!pendingRestart}
    />
  );
  const toggleField = (label: string, field: string, helperText = '') => (
    <ResponsiveSettingItem
      icon={<SettingsIcon />}
      label={label}
      value={loadedForm[field] ? 'Enabled' : 'Disabled'}
      editValue={form[field] ?? false}
      onChange={(value) => handleForm(field, value === 'true')}
      helperText={helperText}
      type="select"
      options={[{ value: 'true', label: 'Enabled' }, { value: 'false', label: 'Disabled' }]}
      showCurrentValue={!pendingRestart}
    />
  );

  const fetchSettings = async () => {
    if (actionFence.current) return;
    const epoch = ++requestEpoch.current;
    setNeedsReload(true);
    try {
      setError(null);
      setLoading(true);
      const data = await client.getSettings();
      if (epoch !== requestEpoch.current) return;
      if (!data._meta?.desired_revision_id) throw new Error('Settings could not be loaded. Click Load Settings to try again.');
      hydrate(data);
      try {
        const cfg = await client.getConfig();
        if (epoch === requestEpoch.current) setDocsBase(cfg?.branding?.docs_base || DEFAULT_DOCS_BASE);
      } catch { /* Settings remain usable when branding is unavailable. */ }
    } catch (err) {
      if (epoch === requestEpoch.current) setError(err instanceof Error ? err.message : 'Failed to fetch settings');
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
    onFocused?.();
  }, [settings, focus, onFocused]);

  const exportEnv = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.exportSettings();
      setEnvContent(data.env);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to export settings');
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

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          Settings
        </Typography>
        <Box>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchSettings}
            disabled={loading}
            sx={{ mr: 1 }}
          >
            Load Settings
          </Button>
          <Button variant="contained" onClick={exportEnv} disabled={loading} sx={{ mr: 1 }}>
            Export .env
          </Button>
          <Button
            variant="outlined"
            onClick={async () => {
              try {
                setLoading(true); setError(null);
                const res = await client.persistSettings();
                setSnack(`Recovery .env saved to ${res.path}. A full backup also needs the database and installation key.`);
              } catch (e: any) {
                setError(e?.message || 'Failed to save on server');
              } finally { setLoading(false); }
            }}
            disabled={loading}
          >
            Write recovery .env
          </Button>

        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}
      {settings && needsReload && !loading && !error ? (
        <Alert severity="warning" sx={{ mb: 3 }}>
          Editing is paused. Click Load Settings to continue.
        </Alert>
      ) : settings && pendingRestart && !needsReload ? (
        <Alert severity="warning" sx={{ mb: 3 }}>
          {restartMessage}
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
          {/* Backend Configuration */}
          <ResponsiveFormSection
            title="Backend Configuration"
            subtitle="Choose the default provider and optional outbound and inbound overrides"
            icon={<CloudIcon />}
          >
            <ResponsiveSettingItem
              icon={<CloudIcon />}
              label="Default Provider"
              value={settings.backend.type.toUpperCase()}
              editValue={form.backend ?? settings.backend.type}
              onChange={(value) => handleForm('backend', value)}
              helperText="Used for sending and receiving unless an override is set below."
              type="select"
              options={[
                { value: 'phaxio', label: 'Phaxio' },
                { value: 'sinch', label: 'Sinch' },
                { value: 'signalwire', label: 'SignalWire' },
                { value: 'documo', label: 'Documo' },
                { value: 'humblefax', label: 'HumbleFax' },
                { value: 'sip', label: 'SIP/Asterisk' },
                { value: 'freeswitch', label: 'FreeSWITCH' }
              ]}
              showCurrentValue={!pendingRestart}
            />
            <ResponsiveSettingItem
              icon={<CloudIcon />}
              label="Outbound Provider"
              value={loadedOutbound.toUpperCase()}
              editValue={form.outbound_backend ?? ''}
              helperText="Provider used to send faxes."
              onChange={(value) => handleForm('outbound_backend', value)}
              type="select"
              options={[
                { value: '', label: `Inherit default provider (${String(form.backend)})` },
                { value: 'phaxio', label: 'Phaxio (Cloud)' },
                { value: 'sinch', label: 'Sinch (Cloud)' },
                { value: 'signalwire', label: 'SignalWire (Cloud)' },
                { value: 'documo', label: 'Documo (Cloud)' },
                { value: 'humblefax', label: 'HumbleFax (Cloud)' },
                { value: 'sip', label: 'SIP/Asterisk (Self-hosted)' },
                { value: 'freeswitch', label: 'FreeSWITCH (Self-hosted)' }
              ]}
              showCurrentValue={!pendingRestart}
            />

            <ResponsiveSettingItem
              icon={<CloudIcon />}
              label="Inbound Provider"
              value={loadedInbound.toUpperCase()}
              editValue={form.inbound_backend ?? ''}
              helperText="Provider used to receive faxes: SIP/Asterisk for your own phone system, or a cloud provider."
              onChange={(value) => handleForm('inbound_backend', value)}
              type="select"
              options={[
                { value: '', label: `Inherit default provider (${String(form.backend)})` },
                { value: 'phaxio', label: 'Phaxio (Webhook)' },
                { value: 'sinch', label: 'Sinch (Webhook)' },
                { value: 'sip', label: 'SIP/Asterisk (Internal)' }
              ]}
              showCurrentValue={!pendingRestart}
            />
            {form.inbound_backend === '' && (
              <Chip
                label={`Inbound uses the default provider (${String(form.backend)}).`}
                color="info"
                size="small"
                variant="outlined"
                sx={{ mt: 1, borderRadius: 1 }}
              />
            )}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1 }}>
              <Chip
                label={settings.backend.disabled ? 'Fax sending is off' : 'Fax sending is on'}
                color={settings.backend.disabled ? 'error' : 'success'}
                size="small"
                variant="outlined"
                sx={{ borderRadius: 1 }}
              />
            </Box>
          </ResponsiveFormSection>

          {/* Security Settings */}
          <ResponsiveFormSection
            title="Security Settings"
            subtitle="Configure authentication, HTTPS, and audit logging"
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
              label="HTTPS Enforced"
              value={settings.security.enforce_https ? 'Yes' : 'No'}
              editValue={form.enforce_public_https ?? settings.security.enforce_https}
              helperText="Require HTTPS for public document links."
              onChange={(value) => handleForm('enforce_public_https', value === 'true')}
              type="select"
              options={[
                { value: 'true', label: 'Yes (Enforced)' },
                { value: 'false', label: 'No' }
              ]}
              showCurrentValue={!pendingRestart}
            />
            
            <ResponsiveSettingItem
              icon={settings.security.audit_enabled ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
              label="Audit Logging"
              value={settings.security.audit_enabled ? 'Enabled' : 'Disabled'}
              editValue={form.audit_log_enabled ?? settings.security.audit_enabled}
              helperText="Record admin actions and fax activity; view them in the Logs tab."
              onChange={(value) => handleForm('audit_log_enabled', value === 'true')}
              type="select"
              options={[
                { value: 'true', label: 'Enabled' },
                { value: 'false', label: 'Disabled' }
              ]}
              showCurrentValue={!pendingRestart}
            />
            
            <ResponsiveSettingItem
              icon={settings.persisted?.enabled ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
              label="Allow .env import for first bootstrap"
              value={settings.persisted?.enabled ? 'Enabled' : 'Disabled'}
              editValue={form.enable_persisted_settings ?? settings.persisted?.enabled ?? false}
              helperText="Lets a .env file set up a new Faxbot once; later edits to the file are ignored."
              onChange={(value) => handleForm('enable_persisted_settings', value === 'true')}
              type="select"
              options={[
                { value: 'true', label: 'Enabled' },
                { value: 'false', label: 'Disabled' }
              ]}
              showCurrentValue={!pendingRestart}
            />
            {textField('Public API URL', 'public_api_url', 'Public address of this server, used for document links and provider callbacks.')}
            {textField('Audit Log Format', 'audit_log_format')}
            {textField('Audit Log File', 'audit_log_file', 'Leave empty to stop writing audit logs to a file.')}
            {toggleField('Audit Syslog', 'audit_log_syslog')}
            {textField('Audit Syslog Address', 'audit_log_syslog_address')}
          </ResponsiveFormSection>

          {/* VPN Tunnel (iOS connectivity) */}
          <TunnelSettings
            client={client}
            docsBase={docsBase}
            hipaaMode={Boolean(settings.security?.enforce_https && settings.security?.require_api_key)}
          />

          {/* Backend-Specific Configuration */}
          {providerSelected('phaxio') && (
                  <ResponsiveSettingSection
                    title="PHAXIO Configuration"
                    subtitle="Configure your Phaxio API credentials and settings"
                  >
                    <Box sx={{ display: 'flex', gap: 1, mb: 2, flexWrap: 'wrap' }}>
                      <Chip
                        label="Faxbot: Phaxio Setup"
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
                      helperText="Find this in the Phaxio console; keep it secret."
                      placeholder="Update PHAXIO_API_KEY"
                      onChange={(value) => handleForm('phaxio_api_key', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (!!settings.phaxio.api_key)}
                    />
                    
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.phaxio.api_secret)}
                      label="API Secret"
                      value={settings.phaxio.api_secret?.replace(/./g, '*').slice(0, 20) || ''}
                      editValue={form.phaxio_api_secret ?? ''}
                      helperText="Shown next to the API key in the Phaxio console."
                      placeholder="Update PHAXIO_API_SECRET"
                      onChange={(value) => handleForm('phaxio_api_secret', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (!!settings.phaxio.api_secret)}
                    />
                    
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.phaxio.callback_token)}
                      label="Callback Token"
                      value={settings.phaxio.callback_token ?? ''}
                      editValue={form.phaxio_callback_token ?? ''}
                      helperText="The Callback Token from the Phaxio console (not the API secret), used to verify status callbacks."
                      placeholder="Update PHAXIO_CALLBACK_TOKEN"
                      onChange={(value) => handleForm('phaxio_callback_token', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && !!settings.phaxio.callback_token}
                    />

                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.phaxio.callback_url)}
                      label="Outbound Callback URL Override"
                      value={settings.phaxio.callback_url ?? ''}
                      editValue={form.phaxio_status_callback_url ?? ''}
                      helperText="Leave empty to use /phaxio-callback on the Public API URL."
                      placeholder="https://localhost:8080/phaxio-callback"
                      onChange={(value) => handleForm('phaxio_status_callback_url', value)}
                      showCurrentValue={!pendingRestart && (!!settings.phaxio.callback_url)}
                    />
                    {toggleField('Authenticated Phaxio Outbound Callbacks', 'phaxio_verify_signature', 'When off, Phaxio status callbacks are rejected and Faxbot checks status by polling instead.')}
                  </ResponsiveSettingSection>
                )}

                {providerSelected('sinch') && (
                  <ResponsiveSettingSection title="Sinch Configuration" subtitle="Configure your Sinch fax endpoint and credentials">
                    {textField('Sinch Project ID', 'sinch_project_id')}
                    {textField('Sinch Base URL', 'sinch_base_url', 'Leave empty to use the standard Sinch endpoint.')}
                    {textField('Sinch API Key', 'sinch_api_key', 'Leave unchanged to keep the saved key.', 'password')}
                    {textField('Sinch API Secret', 'sinch_api_secret', 'Leave unchanged to keep the saved secret.', 'password')}
                  </ResponsiveSettingSection>
                )}

                {providerSelected('documo') && (
                  <ResponsiveSettingSection
                    title="Documo Configuration"
                    subtitle="Configure your Documo API settings"
                  >
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings?.documo?.configured)}
                      label="Documo API Key"
                      value={settings?.documo?.configured ? 'Configured' : ''}
                      editValue={form.documo_api_key ?? ''}
                      helperText="Enter your Documo API key for authentication."
                      placeholder="DOCUMO_API_KEY"
                      onChange={(value) => handleForm('documo_api_key', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (settings?.documo?.configured)}
                    />
                    
                    {textField('Documo Base URL', 'documo_base_url')}
                    <ResponsiveSettingItem
                      icon={getStatusIcon(true)}
                      label="Sandbox Mode"
                      value={settings.documo?.sandbox ? 'Sandbox' : 'Production'}
                      editValue={form.documo_use_sandbox ?? settings.documo?.sandbox ?? false}
                      helperText="Send through Documo’s sandbox instead of production."
                      onChange={(value) => handleForm('documo_use_sandbox', value === 'true')}
                      type="select"
                      options={[
                        { value: 'false', label: 'Production' },
                        { value: 'true', label: 'Sandbox' }
                      ]}
                      showCurrentValue={false}
                    />
                  </ResponsiveSettingSection>
                )}

                {providerSelected('humblefax') && (
                  <ResponsiveSettingSection
                    title="HumbleFax Configuration"
                    subtitle="Configure your HumbleFax API keys"
                  >
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings?.humblefax?.configured)}
                      label="HumbleFax Access Key"
                      value={settings?.humblefax?.configured ? 'Configured' : ''}
                      editValue={form.humblefax_access_key ?? ''}
                      helperText="Enter the access key from your HumbleFax account."
                      placeholder="HUMBLEFAX_ACCESS_KEY"
                      onChange={(value) => handleForm('humblefax_access_key', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (settings?.humblefax?.configured)}
                    />
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings?.humblefax?.configured)}
                      label="HumbleFax Secret Key"
                      value={settings?.humblefax?.configured ? 'Configured' : ''}
                      editValue={form.humblefax_secret_key ?? ''}
                      helperText="Enter the secret key from your HumbleFax account."
                      placeholder="HUMBLEFAX_SECRET_KEY"
                      onChange={(value) => handleForm('humblefax_secret_key', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (settings?.humblefax?.configured)}
                    />
                    {textField('HumbleFax From Number', 'humblefax_from_number', 'Optional. 10 digits, or 11 digits starting with 1. Leave empty to use the account default number.')}
                  </ResponsiveSettingSection>
                )}

                {providerSelected('sip') && (
                  <ResponsiveSettingSection
                    title="SIP / Asterisk Configuration"
                    subtitle="Configure your Asterisk AMI connection settings"
                  >
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.sip.ami_host)}
                      label="AMI Host"
                      value={settings.sip.ami_host || ''}
                      editValue={form.ami_host ?? ''}
                      helperText='Asterisk service hostname on your private network (e.g., docker compose service name "asterisk").'
                      placeholder="ASTERISK_AMI_HOST"
                      onChange={(value) => handleForm('ami_host', value)}
                      showCurrentValue={!pendingRestart && (!!settings.sip.ami_host)}
                    />
                    
                    {textField('AMI Port', 'ami_port', '', 'number')}
                    {textField('AMI Username', 'ami_username')}
                    <ResponsiveSettingItem
                      icon={settings.sip.ami_password_is_default ? <WarningIcon color="warning" /> : <CheckCircleIcon color="success" />}
                      label="AMI Password"
                      value={settings.sip.ami_password_is_default ? 'Using default (insecure)' : 'Custom password set'}
                      editValue={form.ami_password ?? ''}
                      helperText="Must match Asterisk manager.conf and must not be the default; never expose port 5038 publicly."
                      placeholder="Update ASTERISK_AMI_PASSWORD"
                      onChange={(value) => handleForm('ami_password', value)}
                      type="password"
                      showCurrentValue={!pendingRestart && (!settings.sip.ami_password_is_default)}
                    />
                    
                    <ResponsiveSettingItem
                      icon={getStatusIcon(!!settings.sip.station_id)}
                      label="Station ID"
                      value={settings.sip.station_id || ''}
                      editValue={form.fax_station_id ?? ''}
                      helperText="Your fax header/DID in E.164 format (e.g., +15551234567)."
                      placeholder="FAX_LOCAL_STATION_ID"
                      onChange={(value) => handleForm('fax_station_id', value)}
                      showCurrentValue={!pendingRestart && (!!settings.sip.station_id)}
                    />
                    <SipTrunkSettings client={client} />
                  </ResponsiveSettingSection>
                )}

          {/* Feature Flags */}
          <ResponsiveFormSection
            title="Feature Flags"
            subtitle="Optional features; some take effect after a restart."
            icon={<SettingsIcon />}
          >
            <Stack spacing={2}>
              <FormControlLabel
                control={
                  <Switch
                    checked={Boolean(form.feature_v3_plugins ?? settings?.features?.v3_plugins ?? false)}
                    onChange={(e) => handleForm('feature_v3_plugins', e.target.checked)}
                    sx={{ '& .MuiSwitch-thumb': { width: 20, height: 20 } }}
                  />
                }
                label="Enable v3 Plugin System"
                sx={{ alignItems: 'flex-start', '& .MuiFormControlLabel-label': { mt: 0.5 } }}
              />
              <Typography variant="caption" color="text.secondary" sx={{ ml: 4, mb: 1 }}>
                Activates the new modular plugin architecture for fax providers
              </Typography>
              
              <FormControlLabel
                control={
                  <Switch
                    checked={Boolean(form.fax_disabled ?? settings?.backend?.disabled ?? false)}
                    onChange={(e) => handleForm('fax_disabled', e.target.checked)}
                    sx={{ '& .MuiSwitch-thumb': { width: 20, height: 20 } }}
                  />
                }
                label="Disable outbound fax sending"
                sx={{ alignItems: 'flex-start', '& .MuiFormControlLabel-label': { mt: 0.5 } }}
              />
              <Typography variant="caption" color="text.secondary" sx={{ ml: 4, mb: 1 }}>
                Stops sending; faxes submitted while sending is off stay on hold after you turn it back on.
              </Typography>
              
              <FormControlLabel
                control={
                  <Switch
                    checked={Boolean(form.inbound_enabled ?? settings?.inbound?.enabled ?? false)}
                    onChange={(e) => handleForm('inbound_enabled', e.target.checked)}
                    sx={{ '& .MuiSwitch-thumb': { width: 20, height: 20 } }}
                  />
                }
                label="Enable Inbound Fax Receiving"
                sx={{ alignItems: 'flex-start', '& .MuiFormControlLabel-label': { mt: 0.5 } }}
              />
              <Typography variant="caption" color="text.secondary" sx={{ ml: 4, mb: 1 }}>
                Allow receiving faxes (requires additional configuration based on backend)
              </Typography>

              <FormControlLabel
                control={
                  <Switch
                    checked={Boolean(form.feature_plugin_install ?? settings?.features?.plugin_install ?? false)}
                    onChange={(e) => handleForm('feature_plugin_install', e.target.checked)}
                    disabled
                    sx={{ '& .MuiSwitch-thumb': { width: 20, height: 20 } }}
                  />
                }
                label="Allow Remote Plugin Installation (Advanced)"
                sx={{ alignItems: 'flex-start', '& .MuiFormControlLabel-label': { mt: 0.5 } }}
              />
              <Typography variant="caption" color="text.secondary" sx={{ ml: 4 }}>
                Disabled by default for security. Enable only in trusted environments.
              </Typography>
            </Stack>

          </ResponsiveFormSection>

          {/* Inbound Receiving */}
          <ResponsiveFormSection
            title="Inbound Receiving"
            subtitle="Configure inbound fax receiving and storage settings"
            icon={<CheckCircleIcon />}
          >
            <ResponsiveSettingItem
              icon={settings.inbound?.enabled ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
              label="Enable Inbound"
              value={settings.inbound?.enabled ? 'Enabled' : 'Disabled'}
              editValue={form.inbound_enabled ?? settings.inbound?.enabled ?? false}
              helperText="Allow receiving faxes (requires additional configuration based on backend)"
              onChange={(value) => handleForm('inbound_enabled', value === 'true')}
              type="select"
              options={[
                { value: 'true', label: 'Enabled' },
                { value: 'false', label: 'Disabled' }
              ]}
              showCurrentValue={!pendingRestart}
            />
            {effectiveInbound === 'humblefax' && Boolean(form.inbound_enabled) && (
              <Alert severity="warning">
                HumbleFax cannot receive faxes, so Faxbot will not save receiving with it; choose another inbound provider or turn receiving off.
              </Alert>
            )}

            <ResponsiveSettingItem
              icon={<SettingsIcon />}
              label="Retention Days"
              value={String(settings.inbound?.retention_days ?? 30)}
              editValue={form.inbound_retention_days ?? settings.inbound?.retention_days ?? 30}
              helperText="How long to keep inbound fax files before automatic cleanup"
              onChange={(value) => handleForm('inbound_retention_days', value === '' ? '' : Number(value))}
              type="number"
              placeholder={String(settings.inbound?.retention_days ?? 30)}
              showCurrentValue={!pendingRestart}
            />
            
            <ResponsiveSettingItem
              icon={<SettingsIcon />}
              label="Token TTL (minutes)"
              value={String(settings.inbound?.token_ttl_minutes ?? 60)}
              editValue={form.inbound_token_ttl_minutes ?? settings.inbound?.token_ttl_minutes ?? 60}
              helperText="How long PDF download tokens remain valid"
              onChange={(value) => handleForm('inbound_token_ttl_minutes', value === '' ? '' : Number(value))}
              type="number"
              placeholder={String(settings.inbound?.token_ttl_minutes ?? 60)}
              showCurrentValue={!pendingRestart}
            />

            {effectiveInbound === 'sip' && (
              <Box sx={{ mt: 2 }}>
                <ResponsiveSettingItem
                  icon={<SecurityIcon />}
                  label="Asterisk Inbound Secret"
                  value={lastGeneratedSecret ? 'New secret (copy below)' : (settings.inbound.sip?.configured ? 'Configured' : 'Not configured')}
                  editValue={form.asterisk_inbound_secret ?? ''}
                  helperText="Shared secret your Asterisk dialplan sends when posting inbound faxes to Faxbot; keep it private."
                  onChange={(value) => handleForm('asterisk_inbound_secret', value)}
                  placeholder="ASTERISK_INBOUND_SECRET"
                  type="password"
                  showCurrentValue={false}
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
                      } catch(e:any){ setError(e?.message||'Failed to generate secret'); }
                    }}
                    sx={{ borderRadius: 1 }}
                  >
                    Generate
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
            )}

            {effectiveInbound === 'phaxio' && (
              <ResponsiveSettingItem
                icon={settings.inbound?.phaxio?.verify_signature ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                label="Verify Phaxio Inbound Signature"
                value={settings.inbound?.phaxio?.verify_signature ? 'Enabled' : 'Disabled'}
                editValue={form.phaxio_inbound_verify_signature ?? settings.inbound?.phaxio?.verify_signature ?? false}
                helperText="Enable HMAC signature verification for Phaxio inbound webhooks (recommended for security)"
                onChange={(value) => handleForm('phaxio_inbound_verify_signature', value === 'true')}
                type="select"
                options={[
                  { value: 'true', label: 'Enabled (Recommended)' },
                  { value: 'false', label: 'Disabled' }
                ]}
                showCurrentValue={!pendingRestart}
              />
            )}

            {effectiveInbound === 'sinch' && (
              <Box sx={{ mt: 2 }}>
                <ResponsiveSettingItem
                  icon={settings.inbound?.sinch?.verify_signature ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                  label="Verify Sinch Inbound Signature"
                  value={settings.inbound?.sinch?.verify_signature ? 'Enabled' : 'Disabled'}
                  editValue={form.sinch_inbound_verify_signature ?? settings.inbound?.sinch?.verify_signature ?? false}
                  helperText="Enable HMAC signature verification for Sinch inbound webhooks"
                  onChange={(value) => handleForm('sinch_inbound_verify_signature', value === 'true')}
                  type="select"
                  options={[
                    { value: 'true', label: 'Enabled' },
                    { value: 'false', label: 'Disabled' }
                  ]}
                  showCurrentValue={!pendingRestart}
                />
                
                <ResponsiveSettingItem
                  icon={settings.inbound?.sinch?.basic_auth_configured ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                  label="Sinch Inbound Basic Auth User"
                  value={settings.inbound?.sinch?.basic_auth_configured ? 'Configured' : 'Not configured'}
                  editValue={form.sinch_inbound_basic_user ?? ''}
                  helperText="Optional: require HTTP Basic credentials on Sinch callbacks."
                  onChange={(value) => handleForm('sinch_inbound_basic_user', value)}
                  placeholder="SINCH_INBOUND_BASIC_USER"
                  showCurrentValue={false}
                />
                
                <ResponsiveSettingItem
                  icon={<SecurityIcon />}
                  label="Sinch Inbound Basic Auth Password"
                  value=""
                  editValue={form.sinch_inbound_basic_pass ?? ''}
                  helperText="Password for Basic authentication"
                  onChange={(value) => handleForm('sinch_inbound_basic_pass', value)}
                  placeholder="SINCH_INBOUND_BASIC_PASS"
                  type="password"
                  showCurrentValue={false}
                />
                
                <ResponsiveSettingItem
                  icon={settings.inbound?.sinch?.hmac_configured ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                  label="Sinch Inbound HMAC Secret"
                  value={settings.inbound?.sinch?.hmac_configured ? 'Configured' : 'Not configured'}
                  editValue={form.sinch_inbound_hmac_secret ?? ''}
                  helperText="Optional: verify Sinch callbacks with this shared secret; set the same value in Sinch."
                  onChange={(value) => handleForm('sinch_inbound_hmac_secret', value)}
                  placeholder="SINCH_INBOUND_HMAC_SECRET"
                  type="password"
                  showCurrentValue={false}
                />
              </Box>
            )}
          </ResponsiveFormSection>

          <DeliverySettingsSections client={client} settings={settings} form={form} loaded={loadedForm}
            onChange={handleForm} showCurrentValue={!pendingRestart} outbound={String(effectiveOutbound)} canWrite={canWrite} />

          {/* SignalWire (cloud) */}
          {providerSelected('signalwire') && (
            <ResponsiveFormSection
              title="SignalWire Configuration"
              subtitle="Configure your SignalWire fax settings"
              icon={<CloudIcon />}
            >
              <ResponsiveSettingItem
                icon={<CloudIcon />}
                label="Space URL"
                value={settings.signalwire?.space_url || ''}
                editValue={form.signalwire_space_url ?? ''}
                helperText="Your SignalWire space URL (e.g., example.signalwire.com)"
                onChange={(value) => handleForm('signalwire_space_url', value)}
                placeholder="example.signalwire.com"
                showCurrentValue={!pendingRestart && (!!settings.signalwire?.space_url)}
              />
              
              <ResponsiveSettingItem
                icon={<SettingsIcon />}
                label="Project ID"
                value={settings.signalwire?.project_id || ''}
                editValue={form.signalwire_project_id ?? ''}
                helperText="Your SignalWire project identifier"
                onChange={(value) => handleForm('signalwire_project_id', value)}
                placeholder="SIGNALWIRE_PROJECT_ID"
                showCurrentValue={!pendingRestart && (!!settings.signalwire?.project_id)}
              />
              
              <ResponsiveSettingItem
                icon={<SecurityIcon />}
                label="API Token"
                value={settings.signalwire?.api_token ? '***' : ''}
                editValue={form.signalwire_api_token ?? ''}
                helperText="Your SignalWire API token for authentication"
                onChange={(value) => handleForm('signalwire_api_token', value)}
                placeholder="SIGNALWIRE_API_TOKEN"
                type="password"
                showCurrentValue={!pendingRestart && (!!settings.signalwire?.api_token)}
              />
              
              <ResponsiveSettingItem
                icon={<SettingsIcon />}
                label="From (fax)"
                value={settings.signalwire?.from_fax || ''}
                editValue={form.signalwire_fax_from_e164 ?? ''}
                helperText="Your fax number in E.164 format (e.g., +13035551234)"
                onChange={(value) => handleForm('signalwire_fax_from_e164', value)}
                placeholder="+13035551234"
                showCurrentValue={!pendingRestart && (!!settings.signalwire?.from_fax)}
              />
              {textField('SignalWire Outbound Callback URL Override', 'signalwire_status_callback_url', 'Leave empty to use /signalwire-callback on the Public API URL.')}
              {textField('SignalWire Webhook Signing Key', 'signalwire_webhook_signing_key', 'Leave unchanged to keep the saved signing key.', 'password')}
              {textField('SignalWire From (SMS)', 'signalwire_sms_from_e164', 'Leave unchanged to keep the saved number.')}
              {textField('SignalWire Status Poll Seconds', 'signalwire_status_poll_seconds', 'Zero disables status polling.', 'number')}
            </ResponsiveFormSection>
          )}

          {/* Storage Configuration */}
          {/* FreeSWITCH (self-hosted) */}
          {providerSelected('freeswitch') && (
            <Grid item xs={12}>
              <Card>
                <CardContent>
                  <Typography variant="h6" gutterBottom>FreeSWITCH</Typography>
                  {textField('ESL Host', 'fs_esl_host', 'FreeSWITCH ESL host on the private network.')}
                  {textField('ESL Port', 'fs_esl_port', 'FreeSWITCH ESL port.', 'number')}
                  {textField('ESL Password', 'fs_esl_password', 'Leave unchanged to keep the saved password, or clear it to remove it.', 'password')}
                  {textField('Gateway Name', 'fs_gateway_name')}
                  {textField('Caller ID Number', 'fs_caller_id_number')}
                  {toggleField('Enable FreeSWITCH T.38', 'fs_t38_enable')}
                  <Box sx={{ mt: 2 }}>
                    <Typography variant="subtitle2" gutterBottom>Outbound Result Hook (copyable)</Typography>
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                      Add this to your outbound dialplan (before hangup) to post result details back to Faxbot. Replace YOUR_SECRET with your <code>ASTERISK_INBOUND_SECRET</code> (shared internal secret).
                    </Typography>
                    <Box component="pre" sx={{ p: 1, bgcolor: 'background.default', border: '1px solid', borderColor: 'divider', borderRadius: 1, overflowX: 'auto', fontSize: '0.75rem' }}>
{`<action application="set" data="api_hangup_hook=system curl -s -X POST \
  -H 'Content-Type: application/json' \
  -H 'X-Internal-Secret: YOUR_SECRET' \
  -d '{\"job_id\":\"${'${faxbot_job_id}'}\",\"fax_status\":\"${'${fax_success}'}\",\"fax_result_text\":\"${'${fax_result_text}'}\",\"fax_document_transferred_pages\":${'${fax_document_transferred_pages}'},\"uuid\":\"${'${uuid}'}\"}' \
  http://api:8080/_internal/freeswitch/outbound_result"/>`}
                    </Box>
                    <Button size="small" sx={{ mt: 1 }} onClick={async ()=>{
                      try {
                        const text = `<action application=\"set\" data=\"api_hangup_hook=system curl -s -X POST \\\n+  -H 'Content-Type: application/json' \\\n+  -H 'X-Internal-Secret: YOUR_SECRET' \\\n+  -d '{\\\"job_id\\\":\\\"${'${faxbot_job_id}'}\\\",\\\"fax_status\\\":\\\"${'${fax_success}'}\\\",\\\"fax_result_text\\\":\\\"${'${fax_result_text}'}\\\",\\\"fax_document_transferred_pages\\\":${'${fax_document_transferred_pages}'},\\\"uuid\\\":\\\"${'${uuid}'}\\\"}' \\\n+  http://api:8080/_internal/freeswitch/outbound_result\"/>`;
                        await navigator.clipboard.writeText(text);
                        setSnack('Copied');
                      } catch {}
                    }}>Copy snippet</Button>
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                      Use service name "api" for Docker Compose networking; otherwise set your API host. Ensure your dialplan sets <code>faxbot_job_id</code> (the originate flow sets it automatically).
                    </Typography>
                  </Box>
                </CardContent>
              </Card>
            </Grid>
          )}
          <ResponsiveFormSection
            title="Storage Configuration"
            subtitle="Configure file storage backend and S3 settings"
            icon={<StorageIcon />}
          >
            <ResponsiveSettingItem
              icon={getStatusIcon(settings.storage?.backend === 's3')}
              label="Storage Backend"
              value={(settings.storage?.backend ?? 'local').toUpperCase()}
              editValue={form.storage_backend ?? settings.storage?.backend ?? 'local'}
              helperText="Where fax files are stored: on this server, or in your S3 bucket."
              onChange={(value) => handleForm('storage_backend', value)}
              type="select"
              options={[
                { value: 'local', label: 'Local' },
                { value: 's3', label: 'S3' }
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
                  helperText="Your S3 bucket name for storing fax files"
                  onChange={(value) => handleForm('s3_bucket', value)}
                  placeholder="S3_BUCKET"
                  showCurrentValue={!pendingRestart && (!!settings.storage?.s3_bucket)}
                />
                
                <ResponsiveSettingItem
                  icon={<CloudIcon />}
                  label="S3 Region"
                  value={settings.storage?.s3_region || ''}
                  editValue={form.s3_region ?? ''}
                  helperText="AWS region where your S3 bucket is located"
                  onChange={(value) => handleForm('s3_region', value)}
                  placeholder="S3_REGION"
                  showCurrentValue={!pendingRestart && (!!settings.storage?.s3_region)}
                />
                
                <ResponsiveSettingItem
                  icon={<SettingsIcon />}
                  label="S3 Prefix"
                  value={settings.storage?.s3_prefix || ''}
                  editValue={form.s3_prefix ?? ''}
                  helperText="Optional prefix for organizing files within the bucket"
                  onChange={(value) => handleForm('s3_prefix', value)}
                  placeholder="S3_PREFIX"
                  showCurrentValue={!pendingRestart && (!!settings.storage?.s3_prefix)}
                />
                
                <ResponsiveSettingItem
                  icon={<CloudIcon />}
                  label="S3 Endpoint URL"
                  value={settings.storage?.s3_endpoint_url || ''}
                  editValue={form.s3_endpoint_url ?? ''}
                  helperText="Custom S3 endpoint for S3-compatible services (MinIO, etc.)"
                  onChange={(value) => handleForm('s3_endpoint_url', value)}
                  placeholder="S3_ENDPOINT_URL"
                  showCurrentValue={!pendingRestart && (!!settings.storage?.s3_endpoint_url)}
                />
                
                <ResponsiveSettingItem
                  icon={<SecurityIcon />}
                  label="S3 KMS Key ID"
                  value={settings.storage?.s3_kms_enabled ? 'Configured' : 'Not set'}
                  editValue={form.s3_kms_key_id ?? ''}
                  helperText="Enable server-side encryption with KMS by specifying a CMK (recommended for PHI)"
                  onChange={(value) => handleForm('s3_kms_key_id', value)}
                  placeholder="S3_KMS_KEY_ID"
                  showCurrentValue={false}
                />
                
                <Box sx={{ mt: 2 }}>
                  <Button 
                    variant="outlined" 
                    onClick={async () => { 
                      try { 
                        setLoading(true); 
                        const diag = await (client as any).runDiagnostics?.(); 
                        if (diag?.checks?.storage?.type === 's3') { 
                          const st = diag.checks.storage; 
                          const ok = st.accessible === true || st.bucket_set; 
                          setSnack(ok ? 'S3 validation passed' : ('S3 validation incomplete' + (st.error ? (': ' + st.error) : ''))); 
                        } else { 
                          setSnack('Diagnostics did not include S3 checks. Enable ENABLE_S3_DIAGNOSTICS=true on server for full validation.'); 
                        } 
                      } catch(e: any) { 
                        setError(e?.message || 'S3 validation failed'); 
                      } finally { 
                        setLoading(false); 
                      } 
                    }}
                    sx={{ borderRadius: 2 }}
                  >
                    Validate S3
                  </Button>
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 2, display: 'block', mt: 1 }}>
                    Full validation requires ENABLE_S3_DIAGNOSTICS=true on server and proper AWS credentials via env/role.
                  </Typography>
                </Box>
              </Box>
            )}
          </ResponsiveFormSection>

          <ResponsiveFormSection title="MCP Configuration" subtitle="Connections for AI assistants; changes take effect after a restart." icon={<SettingsIcon />}>
            {toggleField('Enable MCP SSE', 'enable_mcp_sse')}
            {textField('MCP SSE Path', 'mcp_sse_path')}
            {toggleField('Enable MCP HTTP', 'enable_mcp_http')}
            {textField('MCP HTTP Path', 'mcp_http_path')}
            {toggleField('Require MCP OAuth', 'require_mcp_oauth')}
            {textField('OAuth Issuer', 'oauth_issuer')}
            {textField('OAuth Audience', 'oauth_audience')}
            {textField('OAuth JWKS URL', 'oauth_jwks_url')}
          </ResponsiveFormSection>

          {/* Advanced Settings */}
          <ResponsiveFormSection
              title="Advanced Settings"
              subtitle="Database, rate limiting, and upload configuration"
              icon={<SettingsIcon />}
            >
              <Stack spacing={3}>
                <Box>
                  <ResponsiveSettingItem
                    icon={<StorageIcon />}
                    label="Database URL (redacted, read-only)"
                    value={settings.database?.url ?? ''}
                    editValue={settings.database?.url ?? ''}
                    helperText={`Driver: ${settings.database?.scheme ?? 'unknown'}. Credentials are hidden.`}
                    showCurrentValue={false}
                  />
                  <Alert severity="info" sx={{ mt: 2 }}>
                    The database can’t be changed here; moving to a new database is a planned maintenance task.
                  </Alert>
                </Box>

                {/* Upload Limits */}
                <ResponsiveTextField
                  label="Max Upload Size (MB)"
                  value={String(form.max_file_size_mb ?? settings.limits?.max_file_size_mb ?? 10)}
                  onChange={(value) => handleForm('max_file_size_mb', value === '' ? '' : Number(value))}
                  placeholder="10"
                  helperText="Largest document Faxbot accepts; your provider may have its own limit."
                  type="number"
                  icon={<CloudIcon />}
                />

                {/* Rate Limiting */}
                <ResponsiveTextField
                  label="Global Rate Limit (RPM)"
                  value={String(form.max_requests_per_minute ?? settings.limits?.rate_limit_rpm ?? 60)}
                  onChange={(value) => handleForm('max_requests_per_minute', value === '' ? '' : Number(value))}
                  placeholder="60"
                  helperText="Requests per minute allowed for each API key; 0 turns the limit off."
                  type="number"
                  icon={<SecurityIcon />}
                />

                <ResponsiveTextField
                  label="Inbound List RPM"
                  value={String(form.inbound_list_rpm ?? settings.limits?.inbound_list_rpm ?? 30)}
                  onChange={(value) => handleForm('inbound_list_rpm', value === '' ? '' : Number(value))}
                  placeholder="30"
                  helperText="Requests per minute for each API key when listing inbound faxes."
                  type="number"
                  icon={<SecurityIcon />}
                />

                <ResponsiveTextField
                  label="Inbound Get RPM"
                  value={String(form.inbound_get_rpm ?? settings.limits?.inbound_get_rpm ?? 60)}
                  onChange={(value) => handleForm('inbound_get_rpm', value === '' ? '' : Number(value))}
                  placeholder="60"
                  helperText="Rate limit for fetching inbound fax metadata/PDF (per key)."
                  type="number"
                  icon={<SecurityIcon />}
                />

                {textField('PDF Token TTL (minutes)', 'pdf_token_ttl_minutes', '', 'number')}
                {textField('Artifact TTL (days)', 'artifact_ttl_days', 'Days to keep sent fax files; 0 keeps them indefinitely.', 'number')}
                {textField('Cleanup Interval (minutes)', 'cleanup_interval_minutes', '', 'number')}
                <Alert 
                  severity="info" 
                  sx={{ 
                    borderRadius: 2,
                    '& .MuiAlert-icon': { alignItems: 'center' }
                  }}
                >
                  <Typography variant="body2">
                    For HIPAA environments, set reasonable RPM limits and keep upload size within policy.
                  </Typography>
                </Alert>
              </Stack>
            </ResponsiveFormSection>
        </Stack>
        </Box>
        <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
          <Button variant="contained" onClick={applySettings} disabled={!canEdit || changedFields.length === 0}>
            Apply settings
          </Button>
          <Button variant="outlined" startIcon={<RefreshIcon />} onClick={fetchSettings} disabled={loading}>
            Refresh
          </Button>
        </Box>

        </Box>
      ) : (
        <Typography variant="body2" color="text.secondary">
          Click "Load Settings" to view and edit settings.
        </Typography>
      )}

      {envContent && (
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
