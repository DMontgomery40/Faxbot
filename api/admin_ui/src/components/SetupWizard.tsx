import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Box, Card, CardContent, Typography, Stepper, Step, StepLabel, Button,
  TextField, Alert, CircularProgress, Grid, Paper, Chip, Switch,
  FormControlLabel, Link,
} from '@mui/material';
import { ExpandMore as ExpandMoreIcon } from '@mui/icons-material';
import AdminAPIClient, { configurationWriteRejected, plainRefusal } from '../api/client';
import { DeliveryWizardFields, deliveryEditorValues } from './delivery/DeliverySettings';
import { docsLink } from '../docsLinks';
import type { ConfigurationWriteResult, Settings, SettingsPatch, ValidationResult } from '../api/types';
import SecretInput from './common/SecretInput';
import EnvSetField, { environmentManaged } from './common/EnvSetField';
import RestartNotice, { RESTARTED } from './common/RestartFaxbot';
import SipTrunkSettings from './SipTrunkSettings';
import EfaxSettings, { EFAX_FIELDS, efaxEditorValues } from './EfaxSettings';
import { COUNTRY_HELP, CountryField, settingsNumberFormat } from './common/numbers';
import WizardTestFax from './WizardTestFax';
import { directionSummary, providerLabel } from '../providerLabels';
import ProviderDirectionFields, { directionPatch, directionProblem, loadedDirections } from './common/ProviderDirections';

interface SetupWizardProps {
  client: AdminAPIClient;
  onDone?: () => void;
  docsBase?: string;
  // Whether this person may restart Faxbot from the console (host:restart).
  canRestart?: boolean;
}

type FormValue = string | number | boolean;
type WizardConfig = Record<string, FormValue>;
type Notice = { severity: 'success' | 'info' | 'warning' | 'error'; text: string };
type Provider = { id: string; name: string; source?: string; categories?: string[] };
type InboundCallbacks = { backend: string; callbacks: Array<{ name: string; url: string }> };
type CredentialField = { key: string; label: string; secret?: boolean; number?: boolean; helper?: string };

const STEPS = ['Choose Providers', 'Connect Providers', 'Security Settings', 'Delivery Options', 'Finish'];

const credentialFields: Record<string, CredentialField[]> = {
  phaxio: [
    { key: 'phaxio_api_key', label: 'API Key', secret: true },
    { key: 'phaxio_api_secret', label: 'API Secret', secret: true },
    { key: 'phaxio_callback_token', label: 'Callback Token', secret: true, helper: 'Separate Phaxio status callback token; this is not the API Secret.' },
    { key: 'phaxio_status_callback_url', label: 'Status Callback URL', helper: 'Leave empty to derive the callback URL from the Public API URL.' },
  ],
  sinch: [
    { key: 'sinch_project_id', label: 'Project ID' },
    { key: 'sinch_api_key', label: 'API Key', secret: true },
    { key: 'sinch_api_secret', label: 'API Secret', secret: true },
  ],
  signalwire: [
    { key: 'signalwire_space_url', label: 'Space URL' },
    { key: 'signalwire_project_id', label: 'Project ID' },
    { key: 'signalwire_api_token', label: 'API Token', secret: true },
    { key: 'signalwire_fax_from_e164', label: 'From (fax)' },
  ],
  documo: [{ key: 'documo_api_key', label: 'API Key', secret: true }],
  humblefax: [
    { key: 'humblefax_access_key', label: 'Access Key', secret: true },
    { key: 'humblefax_secret_key', label: 'Secret Key', secret: true },
    { key: 'humblefax_from_number', label: 'From Number (optional)', helper: 'Leave empty to use the account default number.' },
  ],
  // eFax has its own section (EfaxSettings).
  efax: [],
  freeswitch: [
    { key: 'fs_gateway_name', label: 'Gateway Name' },
    { key: 'fs_caller_id_number', label: 'Caller ID Number' },
  ],
  sip: [],
};
// The fax engine's manager connection; Faxbot sets it up, so it sits under Advanced.
const amiFields: CredentialField[] = [
  { key: 'ami_host', label: 'Manager host' },
  { key: 'ami_port', label: 'Manager port', number: true },
  { key: 'ami_username', label: 'Manager username' },
  { key: 'ami_password', label: 'Manager password', secret: true },
];
const STATION_FIELD: CredentialField = { key: 'fax_station_id', label: 'Fax station ID',
  helper: 'The number the receiving fax machine shows for faxes you send.' };
const CHECKABLE = new Set(['phaxio', 'sinch']);
const PROVIDER_FIELDS = ['sending', 'receiving'];
const NUMERIC_FIELDS: Record<string, string> = { ami_port: 'Manager port', pdf_token_ttl_minutes: 'PDF Token TTL', intake_smtp_port: 'Email server port' };
const PROVIDER_PENDING = new Set(['fax_backend', 'outbound_backend', 'inbound_backend', 'inbound_enabled', 'provider_profiles', 'plugins']);

const isMask = (value: FormValue) => typeof value === 'string' && /^\*+$/.test(value);
const errorText = (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback;

// The settings patch names, copied from the loaded settings. Sending and
// receiving are the wizard's own two choices; they are saved as the provider
// settings Faxbot stores. The baseline includes masks so unrelated edits never
// submit stored secrets.
function editorValues(data: Settings): WizardConfig {
  return {
    ...loadedDirections(data),
    enforce_public_https: data.security.enforce_https,
    audit_log_enabled: data.security.audit_enabled,
    public_api_url: data.security.public_api_url,
    pdf_token_ttl_minutes: data.limits.pdf_token_ttl_minutes,
    phaxio_api_key: data.phaxio.api_key,
    phaxio_api_secret: data.phaxio.api_secret,
    phaxio_callback_token: data.phaxio.callback_token,
    phaxio_status_callback_url: data.phaxio.callback_url,
    phaxio_verify_signature: data.phaxio.verify_signature,
    sinch_project_id: data.sinch.project_id,
    sinch_api_key: data.sinch.api_key,
    sinch_api_secret: data.sinch.api_secret,
    signalwire_space_url: data.signalwire?.space_url ?? '',
    signalwire_project_id: data.signalwire?.project_id ?? '',
    signalwire_api_token: data.signalwire?.api_token ?? '',
    signalwire_fax_from_e164: data.signalwire?.from_fax ?? '',
    documo_api_key: data.documo?.api_key ?? '',
    documo_use_sandbox: data.documo?.sandbox ?? false,
    humblefax_access_key: data.humblefax?.access_key ?? '',
    humblefax_secret_key: data.humblefax?.secret_key ?? '',
    humblefax_from_number: data.humblefax?.from_number ?? '',
    ...efaxEditorValues(data),
    ami_host: data.sip.ami_host,
    ami_port: data.sip.ami_port,
    ami_username: data.sip.ami_username,
    ami_password: data.sip.ami_password,
    fax_station_id: data.sip.station_id,
    fs_gateway_name: data.fs?.gateway_name ?? '',
    fs_caller_id_number: data.fs?.caller_id_number ?? '',
    ...(data.numbers ? { fax_default_country: data.numbers.default_country } : {}),
    ...deliveryEditorValues(data),
  };
}

// Which wizard fields each step saves when the person leaves it.
function stepFields(step: number, data: Settings | null): string[] {
  if (step === 0) return [...PROVIDER_FIELDS, 'fax_default_country'];
  if (step === 1) {
    return ['public_api_url', 'phaxio_verify_signature', 'documo_use_sandbox', STATION_FIELD.key,
      ...Object.values(credentialFields).flat().map(field => field.key), ...amiFields.map(field => field.key),
      ...EFAX_FIELDS];
  }
  if (step === 2) return ['enforce_public_https', 'audit_log_enabled', 'pdf_token_ttl_minutes'];
  if (step === 3) return data ? Object.keys(deliveryEditorValues(data)) : [];
  return [];
}

function SetupWizard({ client, onDone, docsBase, canRestart = true }: SetupWizardProps) {
  const [activeStep, setActiveStep] = useState(0);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [config, setConfig] = useState<WizardConfig>({});
  const [baseline, setBaseline] = useState<WizardConfig>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [needsReload, setNeedsReload] = useState(false);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [catalogReady, setCatalogReady] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [saveResult, setSaveResult] = useState<ConfigurationWriteResult | null>(null);
  const [validationResults, setValidationResults] = useState<ValidationResult | null>(null);
  const [validationNote, setValidationNote] = useState<string | null>(null);
  const [envContent, setEnvContent] = useState('');
  const [callbacks, setCallbacks] = useState<InboundCallbacks | null>(null);
  const [verifyingInbound, setVerifyingInbound] = useState(false);
  const [inboundObservation, setInboundObservation] = useState<string | null>(null);
  const [trunkDirty, setTrunkDirty] = useState(false);
  const requestEpoch = useRef(0);
  const actionFence = useRef(false);
  const watcherEpoch = useRef(0);
  const watcherTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const downloadTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const baselineRef = useRef(baseline);
  baselineRef.current = baseline;
  const desiredRevision = needsReload ? undefined : settings?._meta?.desired_revision_id;
  const changedFields = Object.keys(config).filter(field => config[field] !== baseline[field]);
  const sending = String(config.sending ?? '');
  const receiving = String(config.receiving ?? '');
  const canEdit = !!settings && !!desiredRevision && !loading && !busy && !needsReload;
  const docsURL = docsLink('home', docsBase);
  // Loaded settings are authoritative; after a save that could not be reloaded, fall back to the save result.
  const pendingRestart = needsReload ? saveResult?._meta.apply_state === 'pending_restart' : settings?._meta?.apply_state === 'pending_restart';
  const pendingFields = needsReload ? [] : settings?._meta?.pending_fields ?? [];
  const pendingCount = pendingFields.length;
  const restartMessage = pendingFields.some(field => PROVIDER_PENDING.has(field)) ?
    'Restart Faxbot to start using these providers.' : pendingCount ?
      `Restart Faxbot to apply ${pendingCount} pending ${pendingCount === 1 ? 'change' : 'changes'}.` :
      'Restart Faxbot to apply pending changes.';
  const showPaused = needsReload && !!settings && !loading && !busy && !loadError &&
    notice?.severity !== 'error' && notice?.severity !== 'warning';

  const stopWatching = useCallback(() => {
    watcherEpoch.current += 1;
    if (watcherTimer.current) clearTimeout(watcherTimer.current);
    watcherTimer.current = null;
    setVerifyingInbound(false);
  }, []);

  const hydrate = (data: Settings) => {
    const values = editorValues(data);
    setSettings(data);
    setConfig(values);
    setBaseline(values);
    setNeedsReload(false);
    setCallbacks(null);
    setInboundObservation(null);
    setValidationResults(null);
    setValidationNote(null);
    setEnvContent('');
  };

  const loadSettings = useCallback(async () => {
    const epoch = ++requestEpoch.current;
    stopWatching();
    setLoading(true);
    actionFence.current = false;
    setBusy(false);
    setLoadError(null);
    setNotice(null);
    setNeedsReload(true);
    const [desired, catalog] = await Promise.allSettled([client.getSettings(), client.listPlugins()]);
    if (epoch !== requestEpoch.current) return;
    if (catalog.status === 'fulfilled') {
      setProviders((Array.isArray(catalog.value.items) ? catalog.value.items : []).filter((item: Provider) =>
        typeof item.id === 'string' && item.categories?.includes('outbound')));
      setCatalogReady(true);
    } else {
      // Without the plugin list only the built-in providers are offered; the current choice is kept.
      setProviders([]);
      setCatalogReady(false);
    }
    if (desired.status === 'fulfilled' && desired.value._meta?.desired_revision_id) {
      hydrate(desired.value);
    } else {
      setNeedsReload(true);
      setLoadError(desired.status === 'rejected' ? errorText(desired.reason, 'Settings could not be loaded.') :
        'Settings could not be loaded. Reload to try again.');
    }
    setLoading(false);
  }, [client, stopWatching]);

  // After Restart now: reload, stay on this step and say that the restart worked.
  const afterRestart = useCallback(async () => {
    await loadSettings();
    setNotice({ severity: 'success', text: RESTARTED });
  }, [loadSettings]);

  // The trunk section saved settings itself: take the new saved values and
  // revision, and keep what the person is still editing here.
  const rebase = useCallback(async () => {
    const epoch = requestEpoch.current;
    try {
      const data = await client.getSettings();
      if (epoch !== requestEpoch.current || !data._meta?.desired_revision_id) return;
      const fresh = editorValues(data);
      setSettings(data);
      setConfig(current => {
        const merged = { ...fresh };
        for (const [field, value] of Object.entries(current)) {
          if (value !== baselineRef.current[field]) merged[field] = value;
        }
        return merged;
      });
      setBaseline(fresh);
      setNeedsReload(false);
    } catch {
      if (epoch === requestEpoch.current) {
        setNeedsReload(true);
        setNotice({ severity: 'warning', text: 'The page could not refresh. Reload before making more changes.' });
      }
    }
  }, [client]);

  useEffect(() => {
    void loadSettings();
    return () => {
      requestEpoch.current += 1;
      watcherEpoch.current += 1;
      if (watcherTimer.current) clearTimeout(watcherTimer.current);
      for (const [url, timer] of downloadTimers.current) {
        clearTimeout(timer);
        URL.revokeObjectURL(url);
      }
      downloadTimers.current.clear();
    };
  }, [loadSettings]);

  const pluginNames = new Map(providers.map(provider => [provider.id, provider.name]));
  const label = (id: string) => providerLabel(id, pluginNames.get(id));
  const manifest = (id: string) => providers.some(provider => provider.id === id && provider.source === 'manifest');
  const builtin = (id: string) => !!credentialFields[id] && !manifest(id) && (!settings?.features?.v3_plugins || catalogReady);

  // Ref fencing takes effect synchronously, before React renders busy controls.
  const beginAction = () => {
    if (!canEdit || actionFence.current) return false;
    actionFence.current = true;
    setBusy(true);
    return true;
  };

  const finishAction = (epoch: number) => {
    if (epoch !== requestEpoch.current) return;
    actionFence.current = false;
    setBusy(false);
  };

  const handleConfigChange = (field: string, value: FormValue) => {
    if (!canEdit || actionFence.current) return;
    stopWatching();
    setConfig(previous => ({ ...previous, [field]: value }));
    setValidationResults(null);
    setValidationNote(null);
    setSaveResult(null);
    setNotice(null);
    setEnvContent('');
  };

  const handleValidate = async (provider: string) => {
    if (!canEdit || actionFence.current) return;
    setValidationResults(null);
    setValidationNote(null);
    setNotice(null);
    const fields = provider === 'phaxio' ? ['phaxio_api_key', 'phaxio_api_secret'] :
      ['sinch_project_id', 'sinch_api_key', 'sinch_api_secret'];
    if (fields.some(field => config[field] === '' || isMask(config[field]))) {
      setValidationNote('Enter the credentials again to check them here, or run Diagnostics to check the saved ones.');
      return;
    }
    const payload: SettingsPatch = { backend: provider };
    for (const field of fields) payload[field] = config[field];
    if (!beginAction()) return;
    const epoch = requestEpoch.current;
    try {
      const result = await client.validateSettings(payload);
      if (epoch !== requestEpoch.current) return;
      setValidationResults(result);
      setValidationNote(provider === 'sinch' ?
        'For Sinch, this only checks that the credentials are filled in; it doesn’t sign in to Sinch.' :
        'These checks test the credentials only; no fax was sent.');
    } catch (error) {
      if (epoch === requestEpoch.current) setNotice({ severity: 'error', text: errorText(error, 'Validation failed.') });
    } finally {
      finishAction(epoch);
    }
  };

  // The patch for one step's changes, or a sentence saying what to fix first.
  const stepPatch = (step: number): SettingsPatch | string => {
    const patch: SettingsPatch = {};
    const fields = stepFields(step, settings).filter(field => config[field] !== baseline[field]);
    for (const field of fields) {
      if (PROVIDER_FIELDS.includes(field)) continue;
      const value = config[field];
      if (isMask(value)) return 'A secret field contains only asterisks; enter a new value or clear it.';
      if (NUMERIC_FIELDS[field] && (value === '' || !Number.isSafeInteger(value) || Number(value) < 1 ||
          (field !== 'pdf_token_ttl_minutes' && Number(value) > 65535))) {
        return `${NUMERIC_FIELDS[field]} must be a positive whole number${field !== 'pdf_token_ttl_minutes' ? ' no greater than 65535' : ''}.`;
      }
      patch[field] = value;
    }
    if (step === 0 && settings && (sending !== baseline.sending || receiving !== baseline.receiving)) {
      const problem = directionProblem({ sending, receiving });
      if (problem) return problem;
      Object.assign(patch, directionPatch({ sending, receiving }, settings));
    }
    return patch;
  };

  // Save what changed on this step; true when nothing is left unsaved.
  const saveStep = async (step: number): Promise<boolean> => {
    if (!canEdit || actionFence.current || !desiredRevision) return false;
    if (step === 1 && trunkDirty) {
      setNotice({ severity: 'warning', text: 'Save the SIP trunk settings first, or undo your changes there.' });
      return false;
    }
    const prepared = stepPatch(step);
    if (typeof prepared === 'string') {
      setNotice({ severity: 'error', text: prepared });
      return false;
    }
    if (!Object.keys(prepared).length) return true;
    if (!beginAction()) return false;
    stopWatching();
    setNotice(null);
    setSaveResult(null);
    const epoch = requestEpoch.current;
    try {
      const result = await client.updateSettings({ expected_revision_id: desiredRevision, ...prepared });
      if (epoch !== requestEpoch.current) return false;
      setSaveResult(result);
      setNeedsReload(true);
      setEnvContent('');
      setNotice({ severity: 'success', text: result.changed ? 'Settings saved.' : 'Nothing changed.' });
      try {
        const desired = await client.getSettings();
        if (epoch !== requestEpoch.current) return false;
        if (!desired._meta?.desired_revision_id) throw new Error('Settings could not be loaded.');
        hydrate(desired);
      } catch {
        if (epoch !== requestEpoch.current) return false;
        setNotice({ severity: 'warning', text: 'The page could not refresh. Reload before making more changes.' });
        return false;
      }
      return true;
    } catch (error) {
      if (epoch !== requestEpoch.current) return false;
      const message = errorText(error, 'Save failed.');
      setNeedsReload(true);
      if (/\b409\b/.test(message)) {
        setNotice({ severity: 'error', text: 'Someone else changed these settings. Your edits are kept here; reload to see the current values.' });
      } else {
        setNotice({ severity: 'error', text: plainRefusal(error) ?
          `${plainRefusal(error)} Your edits are kept here; reload before trying again.` : configurationWriteRejected(error) ?
          `Settings were not saved (${message}). Your edits are kept here; reload before trying again.` :
          'The save could not be confirmed. Reload to check whether your changes were saved.' });
      }
      return false;
    } finally {
      finishAction(epoch);
    }
  };

  // Leaving a step saves it; a step that cannot be saved stays open with the reason.
  const goTo = async (step: number) => {
    if (loading || busy || actionFence.current) return;
    if (await saveStep(activeStep)) {
      stopWatching();
      setActiveStep(step);
    }
  };

  const exportSettings = async () => {
    if (changedFields.length || !beginAction()) return;
    setNotice(null);
    const epoch = requestEpoch.current;
    try {
      const result = await client.exportSettings();
      if (epoch === requestEpoch.current) setEnvContent(result.env);
    } catch (error) {
      if (epoch === requestEpoch.current) setNotice({ severity: 'error', text: errorText(error, 'Export failed.') });
    } finally {
      finishAction(epoch);
    }
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setNotice({ severity: 'success', text: 'Copied.' });
    } catch (error) {
      setNotice({ severity: 'error', text: errorText(error, 'Copy failed. Select and copy the displayed text instead.') });
    }
  };

  const downloadEnv = () => {
    let url: string | undefined;
    const anchor = document.createElement('a');
    try {
      url = URL.createObjectURL(new Blob([envContent], { type: 'text/plain' }));
      anchor.href = url;
      anchor.download = 'faxbot-redacted.env';
      document.body.appendChild(anchor);
      anchor.click();
      const downloadURL = url;
      downloadTimers.current.set(downloadURL, setTimeout(() => {
        URL.revokeObjectURL(downloadURL);
        downloadTimers.current.delete(downloadURL);
      }, 60000));
      setNotice({ severity: 'info', text: 'Download requested. Check your browser downloads or Save dialog.' });
    } catch (error) {
      if (url) URL.revokeObjectURL(url);
      setNotice({ severity: 'error', text: errorText(error, 'Download could not be started.') });
    } finally {
      anchor.remove();
    }
  };

  const loadCallbacks = async () => {
    if (!beginAction()) return;
    setNotice(null);
    const epoch = requestEpoch.current;
    try {
      const result = await client.getInboundCallbacks();
      if (epoch === requestEpoch.current) setCallbacks(result);
    } catch (error) {
      if (epoch === requestEpoch.current) setNotice({ severity: 'error', text: errorText(error, 'Callback details could not be loaded.') });
    } finally {
      finishAction(epoch);
    }
  };

  const simulateInbound = async () => {
    if (!callbacks || !beginAction()) return;
    setNotice(null);
    const epoch = requestEpoch.current;
    try {
      await client.simulateInbound({ backend: callbacks.backend });
      if (epoch === requestEpoch.current) setNotice({ severity: 'info', text: 'A test fax was added to your received faxes.' });
    } catch (error) {
      if (epoch === requestEpoch.current) setNotice({ severity: 'error', text: errorText(error, 'Inbound simulation failed.') });
    } finally {
      finishAction(epoch);
    }
  };

  const startWatching = () => {
    if (!canEdit || actionFence.current) return;
    stopWatching();
    const epoch = watcherEpoch.current;
    const since = new Date().toISOString();
    let polls = 0;
    setInboundObservation(null);
    setVerifyingInbound(true);
    const poll = async () => {
      if (epoch !== watcherEpoch.current) return;
      try {
        const result = await client.getLogs({ event: 'inbound_received', since });
        if (epoch !== watcherEpoch.current) return;
        if (result.items?.length) {
          setInboundObservation(`A received fax arrived through ${label(String(result.items[0].backend || ''))}.`);
          stopWatching();
          return;
        }
      } catch (error) {
        if (epoch !== watcherEpoch.current) return;
        setNotice({ severity: 'error', text: errorText(error, 'Inbound logs could not be read.') });
        stopWatching();
        return;
      }
      if (++polls >= 30) {
        setInboundObservation('No received fax in the last minute.');
        stopWatching();
      } else {
        watcherTimer.current = setTimeout(() => { void poll(); }, 2000);
      }
    };
    void poll();
  };

  const renderValidation = () => (validationNote || validationResults) && (
    <Box sx={{ mt: 2 }}>
      {validationNote && <Alert severity="info">{validationNote}</Alert>}
      {validationResults && <Paper sx={{ p: 2, mt: 1 }}>
        <Typography variant="subtitle1">Checks for {label(validationResults.backend)}</Typography>
        {Object.entries(validationResults.checks || {}).map(([key, value]) => (
          <Box key={key} display="flex" justifyContent="space-between" alignItems="center" sx={{ mt: 1, gap: 2 }}>
            <Typography>{key.replace(/_/g, ' ')}</Typography>
            {typeof value === 'boolean' ?
              <Chip size="small" color={value ? 'success' : 'error'} label={value ? 'Pass' : 'Fail'} /> :
              <Chip size="small" color="warning" label="Review Diagnostics" />}
          </Box>
        ))}
      </Paper>}
    </Box>
  );

  const field = (spec: CredentialField) => (
    <Grid item xs={12} key={spec.key}>
      {spec.secret && environmentManaged(settings).has(spec.key) ? <EnvSetField fullWidth label={spec.label} /> :
        spec.secret ? <SecretInput fullWidth disabled={!canEdit} label={spec.label} value={config[spec.key] ?? ''}
          onChange={value => handleConfigChange(spec.key, value)} helperText={spec.helper} /> :
          <TextField fullWidth disabled={!canEdit} label={spec.label} value={config[spec.key] ?? ''} type={spec.number ? 'number' : 'text'}
            onChange={event => handleConfigChange(spec.key, spec.number && event.target.value !== '' ? Number(event.target.value) : event.target.value)}
            helperText={spec.helper} />}
    </Grid>
  );

  const callbackDetails = () => <Box sx={{ mt: 3 }}>
    <Typography variant="subtitle1">Callback details for receiving</Typography>
    <Typography variant="body2" sx={{ mb: 1 }}>The addresses your provider sends received faxes to.</Typography>
    <Button variant="outlined" onClick={loadCallbacks}>Show callback details</Button>
    {callbacks && <Paper sx={{ p: 2, mt: 2 }}>
      <Typography>Receiving through {label(callbacks.backend)}</Typography>
      {!callbacks.callbacks?.length && <Alert severity="info" sx={{ mt: 1 }}>This provider has no callback address to set up.</Alert>}
      {callbacks.callbacks?.map(callback => <Box key={callback.name} sx={{ mt: 1 }}>
        <Typography>{callback.name}</Typography>
        <Box component="pre" sx={{ overflow: 'auto', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>{callback.url}</Box>
        <Button onClick={() => { void copyToClipboard(callback.url); }}>Copy URL</Button>
      </Box>)}
      <Box sx={{ mt: 2, display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        <Button variant="outlined" onClick={simulateInbound}>Add a test received fax</Button>
        <Button variant="outlined" onClick={startWatching} disabled={verifyingInbound}>{verifyingInbound ? 'Waiting for a received fax…' : 'Wait for a received fax'}</Button>
        {verifyingInbound && <Button onClick={stopWatching}>Stop waiting</Button>}
      </Box>
      {inboundObservation && <Alert severity="info" sx={{ mt: 1 }}>{inboundObservation}</Alert>}
      <Typography variant="caption" sx={{ display: 'block', mt: 2 }}><a href={docsURL} target="_blank" rel="noreferrer">Faxbot Docs</a></Typography>
    </Paper>}
  </Box>;

  // One section per provider in use; a provider used both ways appears once.
  const providerSection = (id: string, heading: string, roles: { sends: boolean; receives: boolean }) => {
    const known = builtin(id);
    const environment = environmentManaged(settings);
    return <Paper variant="outlined" sx={{ p: 2, mt: 2 }} key={id} data-testid={`provider-section-${id}`}>
      <Typography variant="h6" component="h3">{heading}</Typography>
      {!known ? <Alert severity="info" sx={{ mt: 2 }}>Set up this provider in Tools → Plugins.</Alert> : <>
        {id !== 'sip' && <Alert severity="info" sx={{ my: 2 }}>Saved secrets are hidden; leave them unchanged to keep them.</Alert>}
        {id === 'freeswitch' && <Alert severity="info" sx={{ mt: 2 }}>FreeSWITCH also needs mod_spandsp, a gateway and the Faxbot result hook.</Alert>}
        {credentialFields[id].length > 0 && <Grid container spacing={2} sx={{ mt: 0 }}>
          {credentialFields[id].map(field)}
          {id === 'phaxio' && <Grid item xs={12}>
            <FormControlLabel control={<Switch disabled={!canEdit} checked={!!config.phaxio_verify_signature} onChange={event => handleConfigChange('phaxio_verify_signature', event.target.checked)} />} label="Verify outbound status signatures" />
            <Alert severity="info">When off, Phaxio status callbacks are rejected and Faxbot checks status by polling instead.</Alert>
          </Grid>}
          {id === 'documo' && <Grid item xs={12}><FormControlLabel control={<Switch disabled={!canEdit} checked={!!config.documo_use_sandbox} onChange={event => handleConfigChange('documo_use_sandbox', event.target.checked)} />} label="Use Documo sandbox" /></Grid>}
        </Grid>}
        {id === 'efax' && <EfaxSettings values={config} onChange={handleConfigChange} settings={settings} disabled={!canEdit}
          receives={roles.receives} docsHref={docsLink('efax', docsBase)} client={client} />}
        {CHECKABLE.has(id) && <Box sx={{ mt: 2 }}>
          <Button variant="outlined" onClick={() => { void handleValidate(id); }}>Check these credentials</Button>
          {renderValidation()}
        </Box>}
        {id === 'sip' && <>
          <SipTrunkSettings client={client} showCalls={false} revision={desiredRevision} onSaved={rebase}
            onDirtyChange={setTrunkDirty} showReceiving={roles.receives} />
          {roles.sends && <Grid container spacing={2} sx={{ mt: 1 }}>{field(STATION_FIELD)}</Grid>}
          <Accordion disableGutters variant="outlined" sx={{ mt: 2 }}>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}><Typography>Advanced: fax engine connection</Typography></AccordionSummary>
            <AccordionDetails>
              <Typography variant="body2" sx={{ mb: 1 }}>
                {environment.has('ami_password') ? 'The fax engine password is set in .env, which both Faxbot and its fax engine read.' :
                  settings?.sip.ami_password_is_default ? 'Faxbot creates the fax engine password when it first starts with the SIP trunk in use; there is nothing to type.' :
                    settings?.sip.ami_password_shared ? 'Faxbot shares this password with its fax engine; there is nothing to type.' :
                      'Faxbot shares this password with its fax engine when it next starts.'}
                {' '}Change these only for a fax engine you run yourself.
              </Typography>
              <Alert severity="warning" sx={{ mb: 2 }}>Keep the fax engine connection on your private network; never expose its port to the internet.</Alert>
              <Grid container spacing={2}>{amiFields.map(field)}</Grid>
            </AccordionDetails>
          </Accordion>
        </>}
        {roles.receives && id !== 'sip' && id !== 'efax' && callbackDetails()}
      </>}
    </Paper>;
  };

  const renderStepContent = () => {
    if (activeStep === 0) return <Box>
      <Typography variant="h6">Choose Providers</Typography>
      <ProviderDirectionFields value={{ sending, receiving }} disabled={!canEdit} plugins={providers}
        saved={{ sending: String(baseline.sending ?? ''), receiving: String(baseline.receiving ?? '') }}
        onChange={next => {
          if (next.sending !== sending) handleConfigChange('sending', next.sending);
          if (next.receiving !== receiving) handleConfigChange('receiving', next.receiving);
        }} />
      {!sending && !receiving && <Alert severity="warning" sx={{ mt: 2 }} data-testid="no-provider">No fax provider set up yet. <Link href={docsLink('providers', docsBase)} target="_blank" rel="noreferrer">Provider setup</Link></Alert>}
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>Next saves these choices.</Typography>
      {settings?.numbers && <Box sx={{ mt: 3 }}>
        <CountryField label="Installation country" helperText={COUNTRY_HELP} disabled={!canEdit}
          value={String(config.fax_default_country ?? settings.numbers.default_country)}
          countries={settings.numbers.supported_countries}
          onChange={code => handleConfigChange('fax_default_country', code)} />
      </Box>}
    </Box>;

    if (activeStep === 1) {
      const sections: Array<[string, string, { sends: boolean; receives: boolean }]> = [];
      if (sending && sending === receiving) {
        sections.push([sending, `For sending and receiving: ${label(sending)}`, { sends: true, receives: true }]);
      } else {
        if (sending) sections.push([sending, `For sending: ${label(sending)}`, { sends: true, receives: false }]);
        if (receiving) sections.push([receiving, `For receiving: ${label(receiving)}`, { sends: false, receives: true }]);
      }
      const cloud = [sending, receiving].some(id => id && id !== 'sip' && id !== 'freeswitch');
      return <Box>
        <Typography variant="h6" gutterBottom>Connect Providers</Typography>
        {!sections.length && <Alert severity="info">Choose a provider on the first step to connect it here.</Alert>}
        {sections.map(([id, heading, roles]) => providerSection(id, heading, roles))}
        {cloud && <TextField fullWidth disabled={!canEdit} label="Public API URL" value={config.public_api_url ?? ''} sx={{ mt: 3 }}
          onChange={event => handleConfigChange('public_api_url', event.target.value)} helperText="Public address of this server, used by cloud providers to fetch documents and send callbacks." />}
      </Box>;
    }

    if (activeStep === 2) return <Box>
      <Typography variant="h6" gutterBottom>Security Settings</Typography>
      <Alert severity="info" sx={{ mb: 2 }}>Authentication: required. Every request needs a signed-in person or an API key; manage them in Keys and Users.</Alert>
      <Grid container spacing={2}>
        {[
          ['enforce_public_https', 'Enforce Public HTTPS'],
          ['audit_log_enabled', 'Enable Audit Logging'],
        ].map(([name, title]) => <Grid item xs={12} sm={6} key={name}><FormControlLabel control={<Switch disabled={!canEdit} checked={!!config[name]} onChange={event => handleConfigChange(name, event.target.checked)} />} label={title} /></Grid>)}
        <Grid item xs={12} sm={6}><TextField fullWidth disabled={!canEdit} label="PDF Token TTL (minutes)" type="number" value={config.pdf_token_ttl_minutes ?? ''}
          onChange={event => handleConfigChange('pdf_token_ttl_minutes', event.target.value === '' ? '' : Number(event.target.value))} helperText="How long tokenized PDF URLs remain valid" /></Grid>
      </Grid>
    </Box>;

    if (activeStep === 3) return <Box>
      <Typography variant="h6" gutterBottom>Delivery Options</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Optional. You can change these later in Settings.</Typography>
      {settings && <DeliveryWizardFields settings={settings} config={config} baseline={baseline} onChange={handleConfigChange}
        outbound={sending} disabled={!canEdit} />}
    </Box>;

    return <Box>
      <Typography variant="h6" gutterBottom>Finish</Typography>
      <Typography sx={{ mb: 2 }}>{directionSummary(sending, receiving)}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Each step saved its settings when you moved on.</Typography>
      {settings && (sending || receiving) && (pendingRestart
        ? <Alert severity="info" sx={{ mb: 2 }}>Restart Faxbot first; then you can send and receive a test fax here.</Alert>
        : <Box sx={{ mb: 2 }}><WizardTestFax client={client} sending={sending} receiving={receiving}
          numbers={(settings.sip as { trunk?: { dids?: string[] } }).trunk?.dids ?? []}
          numberFormat={settingsNumberFormat(settings)} installation={settings.direct?.organization} /></Box>)}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        <Button variant="outlined" onClick={exportSettings} disabled={changedFields.length > 0}>Export .env Template</Button>
      </Box>
      {envContent && <Box sx={{ mt: 2 }}>
        <Alert severity="info">Secrets are masked; this file is not a full backup.</Alert>
        <Box sx={{ display: 'flex', gap: 1, my: 2 }}><Button variant="outlined" onClick={() => { void copyToClipboard(envContent); }}>Copy</Button><Button variant="outlined" onClick={downloadEnv}>Download</Button></Box>
        <Paper sx={{ p: 2 }}><Box component="pre" sx={{ m: 0, fontSize: '0.875rem', overflow: 'auto' }}>{envContent}</Box></Paper>
      </Box>}
    </Box>;
  };

  const last = activeStep === STEPS.length - 1;
  return <Box>
    <Typography variant="h4" component="h1" gutterBottom>Setup Wizard</Typography>
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 2 }}>
      <Typography>Choose providers, connect them and review security. Each step saves when you move on.</Typography>
      <Button variant="outlined" onClick={() => { if (!actionFence.current) void loadSettings(); }} disabled={loading || busy}>Reload{changedFields.length ? ' (discard changes)' : ''}</Button>
    </Box>
    {loading && <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}><CircularProgress size={24} /><Typography>Loading settings…</Typography></Box>}
    {loadError && <Alert severity="error" sx={{ mb: 2 }}>{loadError}</Alert>}
    {pendingRestart && <Box sx={{ mb: 2 }}><RestartNotice client={client} text={restartMessage} canRestart={canRestart} onBack={afterRestart} /></Box>}
    {notice && <Alert severity={notice.severity} sx={{ mb: 2 }} onClose={() => setNotice(null)}>{notice.text}</Alert>}
    {showPaused && <Alert severity="warning" sx={{ mb: 2 }}>Editing is paused. Reload to continue.</Alert>}
    {settings && <>
      <Stepper activeStep={activeStep} sx={{ mb: 4 }}>{STEPS.map(title => <Step key={title}><StepLabel>{title}</StepLabel></Step>)}</Stepper>
      <Card><CardContent><Box component="fieldset" disabled={!canEdit} sx={{ m: 0, p: 0, border: 0, minWidth: 0 }}>{renderStepContent()}</Box></CardContent></Card>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 3 }}>
        <Button disabled={activeStep === 0 || loading || busy} onClick={() => { void goTo(activeStep - 1); }}>Back</Button>
        {last ? <Button variant="contained" onClick={() => { if (!actionFence.current) onDone?.(); }} disabled={loading || busy || !onDone}>Done</Button> :
          <Button variant="contained" disabled={loading || busy} onClick={() => { void goTo(activeStep + 1); }}>Next</Button>}
      </Box>
    </>}
    {busy && <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mt: 2 }}><CircularProgress size={20} /><Typography>Working…</Typography></Box>}
  </Box>;
}

export default SetupWizard;
