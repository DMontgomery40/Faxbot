import { useCallback, useEffect, useId, useRef, useState } from 'react';
import {
  Box, Card, CardContent, Typography, Stepper, Step, StepLabel, Button,
  TextField, FormControl, InputLabel, Select, MenuItem, Alert,
  CircularProgress, Grid, Paper, Chip, Switch, FormControlLabel,
} from '@mui/material';
import AdminAPIClient from '../api/client';
import type { Settings, SettingsPatch, ValidationResult } from '../api/types';
import SecretInput from './common/SecretInput';

interface SetupWizardProps {
  client: AdminAPIClient;
  onDone?: () => void;
  docsBase?: string;
}

type FormValue = string | number | boolean;
type WizardConfig = Record<string, FormValue>;
type Notice = { severity: 'success' | 'info' | 'warning' | 'error'; text: string };
type Provider = { id: string; name: string; source?: string; categories?: string[] };
type InboundCallbacks = { backend: string; callbacks: Array<{ name: string; url: string }> };
type CredentialField = { key: string; label: string; secret?: boolean; number?: boolean; helper?: string };

const builtins: Provider[] = [
  { id: 'phaxio', name: 'Phaxio Cloud Fax' },
  { id: 'sinch', name: 'Sinch Fax API v3' },
  { id: 'signalwire', name: 'SignalWire (Compatibility Fax API)' },
  { id: 'documo', name: 'Documo (mFax)' },
  { id: 'sip', name: 'SIP/Asterisk' },
  { id: 'freeswitch', name: 'FreeSWITCH' },
];
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
  freeswitch: [
    { key: 'fs_gateway_name', label: 'Gateway Name' },
    { key: 'fs_caller_id_number', label: 'Caller ID Number' },
  ],
  sip: [
    { key: 'ami_host', label: 'AMI Host' },
    { key: 'ami_port', label: 'AMI Port', number: true },
    { key: 'ami_username', label: 'AMI Username' },
    { key: 'ami_password', label: 'AMI Password', secret: true },
    { key: 'fax_station_id', label: 'Station ID / DID' },
  ],
};
const isMask = (value: FormValue) => typeof value === 'string' && /^\*+$/.test(value);
const errorText = (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback;

// These are the canonical patch names, copied from the desired projection.
// The baseline includes masks so unrelated edits never submit stored secrets.
function editorValues(data: Settings): WizardConfig {
  return {
    backend: data.backend.type,
    outbound_backend: data.hybrid?.outbound_override ?? '',
    inbound_backend: data.hybrid?.inbound_override ?? '',
    inbound_enabled: data.inbound.enabled,
    require_api_key: data.security.require_api_key,
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
    ami_host: data.sip.ami_host,
    ami_port: data.sip.ami_port,
    ami_username: data.sip.ami_username,
    ami_password: data.sip.ami_password,
    fax_station_id: data.sip.station_id,
    fs_gateway_name: data.fs?.gateway_name ?? '',
    fs_caller_id_number: data.fs?.caller_id_number ?? '',
  };
}

function SetupWizard({ client, onDone, docsBase }: SetupWizardProps) {
  const fieldId = useId();
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
  const [catalogNotice, setCatalogNotice] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [applyResult, setApplyResult] = useState<Notice | null>(null);
  const [validationResults, setValidationResults] = useState<ValidationResult | null>(null);
  const [validationNote, setValidationNote] = useState<string | null>(null);
  const [envContent, setEnvContent] = useState('');
  const [callbacks, setCallbacks] = useState<InboundCallbacks | null>(null);
  const [verifyingInbound, setVerifyingInbound] = useState(false);
  const [inboundObservation, setInboundObservation] = useState<string | null>(null);
  const requestEpoch = useRef(0);
  const actionFence = useRef(false);
  const watcherEpoch = useRef(0);
  const watcherTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const downloadTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const steps = ['Choose Providers', 'Configure Credentials', 'Security Settings', 'Apply & Export'];
  const desiredRevision = settings?._meta?.desired_revision_id;
  const changedFields = Object.keys(config).filter(field => config[field] !== baseline[field]);
  const ob = String(config.outbound_backend || config.backend || '');
  const ib = String(config.inbound_backend || config.backend || '');
  const canEdit = !!settings && !!desiredRevision && !loading && !busy && !needsReload;
  const manifestSelected = providers.some(provider => provider.id === ob && provider.source === 'manifest');
  const builtinSelected = !!credentialFields[ob] && !manifestSelected &&
    (!settings?.features?.v3_plugins || catalogReady);
  const docsURL = (docsBase || 'https://dmontgomery40.github.io/Faxbot').replace(/\/$/, '');

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
    setApplyResult(null);
    setNotice(null);
    const [desired, catalog] = await Promise.allSettled([client.getSettings(), client.listPlugins()]);
    if (epoch !== requestEpoch.current) return;
    if (catalog.status === 'fulfilled') {
      setProviders((Array.isArray(catalog.value.items) ? catalog.value.items : []).filter((item: Provider) =>
        typeof item.id === 'string' && item.categories?.includes('outbound')));
      setCatalogReady(true);
      setCatalogNotice(null);
    } else {
      setProviders([]);
      setCatalogReady(false);
      setCatalogNotice('Installed provider metadata is unavailable. Existing provider IDs are preserved.');
    }
    if (desired.status === 'fulfilled' && desired.value._meta?.desired_revision_id) {
      hydrate(desired.value);
    } else {
      setNeedsReload(true);
      setLoadError(desired.status === 'rejected' ? errorText(desired.reason, 'Settings could not be loaded.') :
        'The server did not return a canonical desired revision. Reload before editing.');
    }
    setLoading(false);
  }, [client, stopWatching]);

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

  const providerOptions = new Map(builtins.map(provider => [provider.id, provider]));
  for (const provider of providers) providerOptions.set(provider.id, provider);
  for (const id of [config.backend, config.outbound_backend, config.inbound_backend,
    baseline.backend, baseline.outbound_backend, baseline.inbound_backend]) {
    if (typeof id === 'string' && id && !providerOptions.has(id)) {
      providerOptions.set(id, { id, name: `${id} (current provider)` });
    }
  }

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
    setApplyResult(null);
    setNotice(null);
    setEnvContent('');
  };

  const handleValidate = async () => {
    if (!canEdit || actionFence.current) return;
    setValidationResults(null);
    setValidationNote(null);
    setNotice(null);
    if (!builtinSelected || !['phaxio', 'sinch', 'sip'].includes(ob)) {
      setValidationNote('Setup has no credential check for this provider. Configure it in Tools → Plugins and inspect Diagnostics; no provider or fax validation was performed.');
      return;
    }
    const fields = ob === 'phaxio' ? ['phaxio_api_key', 'phaxio_api_secret'] :
      ob === 'sinch' ? ['sinch_project_id', 'sinch_api_key', 'sinch_api_secret'] :
        ['ami_host', 'ami_port', 'ami_username', 'ami_password'];
    if (fields.some(field => config[field] === '' || isMask(config[field]))) {
      setValidationNote('Enter fresh credentials to run this check. Stored secret masks cannot be validated here; unchanged credentials remain saved. Inspect Diagnostics for the active configuration.');
      return;
    }
    if (ob === 'sip' && (!Number.isSafeInteger(config.ami_port) || Number(config.ami_port) < 1 || Number(config.ami_port) > 65535)) {
      setNotice({ severity: 'error', text: 'AMI Port must be a whole number from 1 to 65535.' });
      return;
    }
    const payload: SettingsPatch = { backend: ob };
    for (const field of fields) payload[field] = config[field];
    if (!beginAction()) return;
    const epoch = requestEpoch.current;
    try {
      const result = await client.validateSettings(payload);
      if (epoch !== requestEpoch.current) return;
      setValidationResults(result);
      setValidationNote(ob === 'sinch' ?
        'Sinch checks credential presence only; it does not authenticate with Sinch. No fax was sent and inbound delivery was not tested.' :
        'These checks use the supplied credentials and local prerequisites. No fax was sent; they do not verify fax delivery or inbound document readiness.');
    } catch (error) {
      if (epoch === requestEpoch.current) setNotice({ severity: 'error', text: errorText(error, 'Validation failed.') });
    } finally {
      finishAction(epoch);
    }
  };

  const applySettings = async () => {
    if (!canEdit || actionFence.current || !desiredRevision) return;
    if (!changedFields.length) {
      setApplyResult({ severity: 'info', text: 'No changes to save. The loaded desired revision is unchanged.' });
      return;
    }
    const payload: SettingsPatch = { expected_revision_id: desiredRevision };
    for (const field of changedFields) {
      const value = config[field];
      if (isMask(value)) {
        setNotice({ severity: 'error', text: 'A changed field contains a stored mask. Reload it, enter a replacement, or explicitly clear it.' });
        return;
      }
      if (['ami_port', 'pdf_token_ttl_minutes'].includes(field) &&
          (value === '' || !Number.isSafeInteger(value) || Number(value) < 1 ||
            (field === 'ami_port' && Number(value) > 65535))) {
        setNotice({ severity: 'error', text: `${field === 'ami_port' ? 'AMI Port' : 'PDF Token TTL'} must be a positive whole number${field === 'ami_port' ? ' no greater than 65535' : ''}.` });
        return;
      }
      payload[field] = value;
    }
    if (!beginAction()) return;
    stopWatching();
    setNotice(null);
    setApplyResult(null);
    const epoch = requestEpoch.current;
    try {
      const result = await client.updateSettings(payload);
      if (epoch !== requestEpoch.current) return;
      if (!result._meta?.desired_revision_id) {
        setNeedsReload(true);
        setNotice({ severity: 'warning', text: 'The server returned no revision after saving. The outcome must be checked with Reload before another save.' });
        return;
      }
      hydrate(result);
      setApplyResult(result._meta.apply_state === 'pending_restart' ?
        { severity: 'warning', text: 'Desired configuration saved durably. Every API worker must stop and the installation restart for the pending changes; active settings remain in effect for those fields.' } :
        { severity: 'success', text: 'Configuration saved durably and active. Existing fax attempts retain their captured settings.' });
    } catch (error) {
      if (epoch !== requestEpoch.current) return;
      const message = errorText(error, 'Save failed.');
      if (/\b409\b/.test(message)) {
        setNeedsReload(true);
        setNotice({ severity: 'error', text: 'Settings changed after this editor loaded. Your draft is retained. Reload explicitly to review the current revision before saving again.' });
      } else {
        setNotice({ severity: 'error', text: message });
      }
    } finally {
      finishAction(epoch);
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
      const result = await client.simulateInbound({ backend: callbacks.backend });
      if (epoch === requestEpoch.current) setNotice({ severity: 'info', text: `Synthetic inbound record created (${result.id}). This does not verify a provider receipt or a usable document.` });
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
          setInboundObservation(`New inbound log event observed (${String(result.items[0].backend || 'unknown provider')}). This is not proof of document readiness.`);
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
        setInboundObservation('No new inbound log event observed during this watch. This does not determine provider or document readiness.');
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
        <Typography variant="subtitle1">Checks for {validationResults.backend}</Typography>
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

  const providerControl = (field: string, label: string, override = false) => (
    <FormControl fullWidth sx={{ mt: 2 }}>
      <InputLabel id={`${fieldId}-${field}-label`} shrink>{label}</InputLabel>
      <Select id={`${fieldId}-${field}`} labelId={`${fieldId}-${field}-label`} displayEmpty
        value={config[field] ?? ''} disabled={!canEdit} label={label} onChange={event => handleConfigChange(field, event.target.value)}>
        {override && <MenuItem value="">Use default provider ({String(config.backend)})</MenuItem>}
        {Array.from(providerOptions.values()).map(provider => <MenuItem key={provider.id} value={provider.id}>{provider.name}</MenuItem>)}
      </Select>
    </FormControl>
  );

  const renderStepContent = () => {
    if (activeStep === 0) return <Box>
      <Typography variant="h6">Choose Providers</Typography>
      {providerControl('backend', 'Default Provider')}
      {providerControl('outbound_backend', 'Outbound Override', true)}
      {providerControl('inbound_backend', 'Inbound Override', true)}
      <Typography sx={{ mt: 2 }}>Desired outbound: {ob} · Desired inbound: {ib}</Typography>
      <FormControlLabel control={<Switch disabled={!canEdit} checked={!!config.inbound_enabled} onChange={event => handleConfigChange('inbound_enabled', event.target.checked)} />} label="Enable inbound handling" />
      <Alert severity="info" sx={{ mt: 1 }}>Each empty override inherits the default provider independently. Choosing an inbound provider does not enable inbound handling or verify receipt.</Alert>
    </Box>;

    if (activeStep === 1) return <Box>
      <Typography variant="h6" gutterBottom>Configure {providerOptions.get(ob)?.name || ob} Credentials</Typography>
      <Alert severity="info" sx={{ mb: 2 }}>Stored values are masked. Leave a mask unchanged to preserve it; replace it or clear the field explicitly to change it. These fields edit the desired revision.</Alert>
      <Button variant="outlined" onClick={handleValidate}>Check Supplied Outbound Credentials</Button>
      {renderValidation()}
      {!builtinSelected ? <Alert severity="info" sx={{ mt: 2 }}>This provider's credential panel is not available in Setup. Keep its selection and configure it in Tools → Plugins; use Settings for other shared fields.</Alert> : <>
        {ob === 'sip' && <Alert severity="warning" sx={{ mt: 2 }}>Keep AMI on the private network. Do not expose its port to the internet.</Alert>}
        {ob === 'freeswitch' && <Alert severity="info" sx={{ mt: 2 }}>FreeSWITCH requires mod_spandsp, a gateway and the Faxbot result hook. These fields alone do not verify that deployment.</Alert>}
        <Grid container spacing={2} sx={{ mt: 0 }}>
          {credentialFields[ob].map(field => <Grid item xs={12} key={field.key}>
            {field.secret ? <SecretInput fullWidth disabled={!canEdit} label={field.label} value={config[field.key] ?? ''}
              onChange={value => handleConfigChange(field.key, value)} helperText={field.helper} /> :
              <TextField fullWidth disabled={!canEdit} label={field.label} value={config[field.key] ?? ''} type={field.number ? 'number' : 'text'}
                onChange={event => handleConfigChange(field.key, field.number && event.target.value !== '' ? Number(event.target.value) : event.target.value)} helperText={field.helper} />}
          </Grid>)}
          {ob === 'phaxio' && <Grid item xs={12}>
            <FormControlLabel control={<Switch disabled={!canEdit} checked={!!config.phaxio_verify_signature} onChange={event => handleConfigChange('phaxio_verify_signature', event.target.checked)} />} label="Verify outbound status signatures" />
            <Alert severity="info">When verification is disabled, outbound callback updates are rejected; original-account polling continues. Inbound verification is configured separately in Settings.</Alert>
          </Grid>}
          {ob === 'documo' && <Grid item xs={12}><FormControlLabel control={<Switch disabled={!canEdit} checked={!!config.documo_use_sandbox} onChange={event => handleConfigChange('documo_use_sandbox', event.target.checked)} />} label="Use Documo sandbox" /></Grid>}
          {ob === 'sip' && settings?.sip.ami_password_is_default && config.ami_password === baseline.ami_password &&
            <Grid item xs={12}><Alert severity="warning">The stored AMI password is still the default. Enter a replacement before enabling remote AMI access.</Alert></Grid>}
        </Grid>
      </>}
      <TextField fullWidth disabled={!canEdit} label="Public API URL" value={config.public_api_url ?? ''} sx={{ mt: 2 }}
        onChange={event => handleConfigChange('public_api_url', event.target.value)} helperText="Used by hosted providers to fetch documents and derive callback URLs. Review HTTPS and reachability separately." />
      <Box sx={{ mt: 3 }}>
        <Typography variant="subtitle1">Active Inbound Callback Details</Typography>
        <Typography variant="body2" sx={{ mb: 1 }}>These details come from active settings, independently of the unsaved desired choices above.</Typography>
        <Button variant="outlined" onClick={loadCallbacks}>Show Active Callback Details</Button>
        {callbacks && <Paper sx={{ p: 2, mt: 2 }}>
          <Typography>Active inbound provider: {callbacks.backend}</Typography>
          {!callbacks.callbacks?.length && <Alert severity="info" sx={{ mt: 1 }}>No callback URL is available from this route for the active provider.</Alert>}
          {callbacks.callbacks?.map(callback => <Box key={callback.name} sx={{ mt: 1 }}>
            <Typography>{callback.name}</Typography>
            <Box component="pre" sx={{ overflow: 'auto', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>{callback.url}</Box>
            <Button onClick={() => { void copyToClipboard(callback.url); }}>Copy URL</Button>
          </Box>)}
          <Alert severity="info" sx={{ mt: 1 }}>Callback URLs and log events do not verify an inbound document. Simulation creates a synthetic record, not a provider delivery.</Alert>
          <Box sx={{ mt: 2, display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            <Button variant="outlined" onClick={simulateInbound}>Simulate Inbound Record</Button>
            <Button variant="outlined" onClick={startWatching} disabled={verifyingInbound}>{verifyingInbound ? 'Watching inbound logs…' : 'Watch New Inbound Log Events'}</Button>
            {verifyingInbound && <Button onClick={stopWatching}>Stop Watching</Button>}
          </Box>
          {inboundObservation && <Alert severity="info" sx={{ mt: 1 }}>{inboundObservation}</Alert>}
          <Typography variant="caption" sx={{ display: 'block', mt: 2 }}><a href={`${docsURL}/`} target="_blank" rel="noreferrer">Faxbot Docs</a> · <a href="https://developers.sinch.com/docs/fax/api-reference/" target="_blank" rel="noreferrer">Sinch Fax API Docs</a></Typography>
        </Paper>}
      </Box>
    </Box>;

    if (activeStep === 2) return <Box>
      <Typography variant="h6" gutterBottom>Security Settings</Typography>
      <Alert severity="info" sx={{ mb: 2 }}>Review access, transport and audit settings for this installation. These controls alone do not establish compliance.</Alert>
      <Grid container spacing={2}>
        {[
          ['require_api_key', 'Require API Key'],
          ['enforce_public_https', 'Enforce Public HTTPS'],
          ['audit_log_enabled', 'Enable Audit Logging'],
        ].map(([field, label]) => <Grid item xs={12} sm={6} key={field}><FormControlLabel control={<Switch disabled={!canEdit} checked={!!config[field]} onChange={event => handleConfigChange(field, event.target.checked)} />} label={label} /></Grid>)}
        <Grid item xs={12} sm={6}><TextField fullWidth disabled={!canEdit} label="PDF Token TTL (minutes)" type="number" value={config.pdf_token_ttl_minutes ?? ''}
          onChange={event => handleConfigChange('pdf_token_ttl_minutes', event.target.value === '' ? '' : Number(event.target.value))} helperText="How long tokenized PDF URLs remain valid" /></Grid>
      </Grid>
    </Box>;

    return <Box>
      <Typography variant="h6" gutterBottom>Apply & Export</Typography>
      <Typography sx={{ mb: 2 }}>{changedFields.length ? `${changedFields.length} changed field(s). Only those changes will be saved.` : 'No unsaved changes.'}</Typography>
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        <Button variant="outlined" onClick={handleValidate}>Check Supplied Outbound Credentials</Button>
        <Button variant="contained" onClick={applySettings}>Apply Changes</Button>
        <Button variant="outlined" onClick={exportSettings} disabled={changedFields.length > 0}>Export Redacted Desired Template</Button>
      </Box>
      {changedFields.length > 0 && <Alert severity="info" sx={{ mt: 2 }}>Save or explicitly Reload to discard your draft before exporting. Export contains the server's current desired settings, not unsaved values.</Alert>}
      {renderValidation()}
      {envContent && <Box sx={{ mt: 2 }}>
        <Alert severity="info">Redacted template of the server's current desired configuration. Secret values are redacted; this is not a complete recovery backup and pending settings may differ from active settings.</Alert>
        <Box sx={{ display: 'flex', gap: 1, my: 2 }}><Button variant="outlined" onClick={() => { void copyToClipboard(envContent); }}>Copy</Button><Button variant="outlined" onClick={downloadEnv}>Download</Button></Box>
        <Paper sx={{ p: 2 }}><Box component="pre" sx={{ m: 0, fontSize: '0.875rem', overflow: 'auto' }}>{envContent}</Box></Paper>
      </Box>}
    </Box>;
  };

  return <Box>
    <Typography variant="h4" component="h1" gutterBottom>Setup Wizard</Typography>
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 2 }}>
      <Typography>Edit the canonical desired configuration.</Typography>
      <Button variant="outlined" onClick={() => { if (!actionFence.current) void loadSettings(); }} disabled={loading || busy}>Reload{changedFields.length ? ' (discard draft)' : ''}</Button>
    </Box>
    {loading && <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}><CircularProgress size={24} /><Typography>Loading desired settings…</Typography></Box>}
    {loadError && <Alert severity="error" sx={{ mb: 2 }}>{loadError}</Alert>}
    {settings?._meta && <Paper sx={{ p: 2, mb: 2, overflowWrap: 'anywhere' }}>
      <Typography variant="body2">Active revision: {settings._meta.active_revision_id} · Generation: {settings._meta.generation}</Typography>
      <Typography variant="body2">Loaded desired revision: {settings._meta.desired_revision_id}</Typography>
      {settings._meta.apply_state === 'pending_restart' ? <Alert severity="warning" sx={{ mt: 1 }}>Every API worker must stop and the installation restart to activate these pending desired fields: {settings._meta.pending_fields.join(', ')}.</Alert> : <Typography variant="body2">Loaded desired revision is active.</Typography>}
    </Paper>}
    {catalogNotice && <Alert severity="info" sx={{ mb: 2 }}>{catalogNotice}</Alert>}
    {notice && <Alert severity={notice.severity} sx={{ mb: 2 }} onClose={() => setNotice(null)}>{notice.text}</Alert>}
    {applyResult && <Alert severity={applyResult.severity} sx={{ mb: 2 }}>{applyResult.text}</Alert>}
    {needsReload && settings && <Alert severity="warning" sx={{ mb: 2 }}>Editing and saving are paused. Your draft remains visible; Reload explicitly discards it and loads the current revision.</Alert>}
    {settings && <>
      <Stepper activeStep={activeStep} sx={{ mb: 4 }}>{steps.map(label => <Step key={label}><StepLabel>{label}</StepLabel></Step>)}</Stepper>
      <Card><CardContent><Box component="fieldset" disabled={!canEdit} sx={{ m: 0, p: 0, border: 0, minWidth: 0 }}>{renderStepContent()}</Box></CardContent></Card>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 3 }}>
        <Button disabled={activeStep === 0 || loading || busy} onClick={() => { stopWatching(); setActiveStep(step => step - 1); }}>Back</Button>
        {activeStep === steps.length - 1 ? <Button variant="contained" onClick={() => { if (!actionFence.current) onDone?.(); }} disabled={loading || busy || !onDone}>{changedFields.length ? 'Done (discard draft)' : 'Done'}</Button> :
          <Button variant="contained" disabled={loading || busy} onClick={() => { stopWatching(); setActiveStep(step => step + 1); }}>Next</Button>}
      </Box>
    </>}
    {busy && <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mt: 2 }}><CircularProgress size={20} /><Typography>Working…</Typography></Box>}
  </Box>;
}

export default SetupWizard;
