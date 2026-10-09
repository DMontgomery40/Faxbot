import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, Checkbox, Chip, CircularProgress, FormControlLabel, MenuItem, Paper, Stack,
  Switch, TextField, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../api/client';
import type { AnalysisSettings, AnalysisStatus } from '../api/analysisTypes';
import type { Settings, SettingsPatch } from '../api/types';
import { formatServerTime } from '../api/time';
import type { AdminDestination } from '../navigation';
import { ScreenHeader } from './access/AccessViews';
import EnvSetField, { environmentManaged } from './common/EnvSetField';

const DEFAULTS: AnalysisSettings = { enabled: false, provider: 'openai', base_url: 'https://api.openai.com/v1',
  model: '', api_key: '', interval_hours: 24, configured: false };
const PATCH_FIELDS = { enabled: 'analysis_enabled', provider: 'analysis_provider', base_url: 'analysis_base_url',
  model: 'analysis_model' } as const;
const PROVIDERS = { openai: 'OpenAI', openrouter: 'OpenRouter', compatible: 'Compatible service' };
const ENDPOINTS = { openai: 'https://api.openai.com/v1', openrouter: 'https://openrouter.ai/api/v1', compatible: '' };
const STATES: Record<AnalysisStatus['state'], string> = {
  not_configured: 'Add your provider, model and API key in AI analysis settings to get started.',
  disabled: 'AI analysis is off. Enable it in settings when you want to use it.',
  idle: 'Ready for an analysis. No result has been generated yet.',
  queued: 'Analysis is queued. This page updates when it finishes.',
  running: 'Analysis is running. This page updates when it finishes.',
  succeeded: 'The latest analysis is ready.', failed: 'The last analysis did not finish. You can try again.',
};
function failure(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && (error.status === 401 || error.status === 403)) {
    return 'This account does not have permission for that action.';
  }
  return fallback;
}

// The same live result is available from Overview, Recommendations, Savings and its settings page.
export function AnalysisCard({ client, canRun = false, onNavigate, settingsPage = false, refreshKey = 0,
  actionsDisabled = false, pollMs = 3000 }: {
  client: AdminAPIClient; canRun?: boolean; onNavigate?: (destination: AdminDestination) => void;
  settingsPage?: boolean; refreshKey?: number; actionsDisabled?: boolean; pollMs?: number;
}) {
  const [status, setStatus] = useState<AnalysisStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const fence = useRef(false);
  const epoch = useRef(0);
  const load = useCallback(async () => {
    const request = ++epoch.current;
    try {
      const next = await client.call<AnalysisStatus>({ method: 'GET', path: '/analysis' });
      if (request !== epoch.current) return;
      setStatus(next);
      setError(null);
      setCheckedAt(new Date().toISOString());
    } catch (caught) {
      if (request === epoch.current) setError(failure(caught, 'Could not load AI analysis. Select Refresh analysis status to try again.'));
    }
  }, [client]);
  useEffect(() => {
    void load();
    return () => { epoch.current += 1; };
  }, [load, refreshKey]);
  const running = status?.state === 'queued' || status?.state === 'running';
  useEffect(() => {
    let cancelled = false;
    let timer: number;
    let generation = 0;
    // Scheduled runs and freshness changes must also reach an already-open card.
    const delay = running ? pollMs : 45000;
    const visible = () => document.visibilityState !== 'hidden';
    const poll = async (request: number) => {
      if (cancelled || request !== generation || !visible()) return;
      if (!fence.current) await load();
      if (!cancelled && request === generation && visible()) {
        timer = window.setTimeout(() => { void poll(request); }, delay);
      }
    };
    const visibilityChanged = () => {
      window.clearTimeout(timer);
      generation += 1;
      if (visible()) void poll(generation);
    };
    if (visible()) {
      timer = window.setTimeout(() => { void poll(generation); }, delay);
    }
    document.addEventListener('visibilitychange', visibilityChanged);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', visibilityChanged);
    };
  }, [running, load, pollMs]);
  const run = async () => {
    if (fence.current || !canRun || !status?.configured || !status.enabled || running || actionsDisabled) return;
    fence.current = true;
    setBusy(true);
    setError(null);
    ++epoch.current;
    try {
      setStatus(await client.call<AnalysisStatus>({ method: 'POST', path: '/analysis/run' }));
    } catch (caught) {
      await load();
      setError(failure(caught, 'Could not confirm that analysis started. Check its status before trying again.'));
    } finally {
      fence.current = false;
      setBusy(false);
    }
  };
  const openSettings = () => {
    if (onNavigate) onNavigate('system/analysis');
    else window.location.hash = '#/system/analysis';
  };
  const runResult = status?.last_run;
  return (
    <Paper component="section" variant="outlined" sx={{ p: 2, mb: 3, borderRadius: 2 }} aria-label="AI analysis">
      <Stack spacing={1.5}>
        <Box display="flex" alignItems="center" gap={1}>
          <Typography variant="h6" component="h2">AI analysis</Typography>
          <Chip size="small" label="Generated advice" variant="outlined" />
        </Box>
        <Typography variant="body2" color="text.secondary">
          Reviews recorded costs and delivery outcomes. Check its suggestions against the figures; it does not change your settings or send faxes.
        </Typography>
        {error && <Alert severity="error">{error}</Alert>}
        {!status && !error && <CircularProgress size={22} aria-label="Loading analysis" />}
        {status && <>
          <Typography role={running ? 'status' : undefined}>{STATES[status.state]}</Typography>
          {status.message && <Alert severity={status.state === 'failed' ? 'error' : 'info'}>{status.message}</Alert>}
          {status.stale && runResult?.summary && <Alert severity="warning">This result uses older evidence or settings. Run a new analysis before acting on it.</Alert>}
          {runResult?.summary && <Typography sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{runResult.summary}</Typography>}
          {runResult && <Typography variant="caption" color="text.secondary">
            {runResult.finished_at ? `Last completed ${formatServerTime(runResult.finished_at)}`
              : `Started ${formatServerTime(runResult.started_at)}`} · {PROVIDERS[runResult.provider as keyof typeof PROVIDERS] ?? runResult.provider} · {runResult.model}
          </Typography>}
          {!!runResult?.evidence.length && <Box component="details">
            <Typography component="summary" variant="body2" sx={{ cursor: 'pointer' }}>Evidence reviewed</Typography>
            <Stack spacing={1} sx={{ mt: 1 }}>
              {runResult.evidence.map((entry, index) => <Box key={index}>
                <Typography variant="body2">{String(entry.label ?? 'Operational evidence')}</Typography>
                {typeof entry.collected_at === 'string' && <Typography variant="caption" color="text.secondary">
                  Collected {formatServerTime(entry.collected_at)}
                </Typography>}
              </Box>)}
            </Stack>
          </Box>}
          {runResult?.error && runResult.error !== status.message && <Alert severity="error">{runResult.error}</Alert>}
          {status.next_run_at && <Typography variant="body2" color="text.secondary">Next scheduled analysis: {formatServerTime(status.next_run_at)}</Typography>}
        </>}
        <Box display="flex" flexWrap="wrap" gap={1}>
          {canRun && <Button variant="contained" onClick={() => void run()}
            disabled={busy || running || !status?.configured || !status.enabled || actionsDisabled}>Run analysis</Button>}
          <Button onClick={() => void load()} disabled={busy}>Refresh analysis status</Button>
          {!settingsPage && <Button onClick={openSettings}>AI analysis settings</Button>}
        </Box>
        {checkedAt && <Typography variant="caption" color="text.secondary">Status checked {formatServerTime(checkedAt)}</Typography>}
      </Stack>
    </Paper>
  );
}

export default function AIAnalysis({ client, isOwner = false }: { client: AdminAPIClient; isOwner?: boolean }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [form, setForm] = useState<AnalysisSettings>(DEFAULTS);
  const [key, setKey] = useState('');
  const [removeKey, setRemoveKey] = useState(false);
  const [hours, setHours] = useState('24');
  const [busy, setBusy] = useState(false);
  const [needsReload, setNeedsReload] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const fence = useRef(false);
  const epoch = useRef(0);
  const load = useCallback(async () => {
    const request = ++epoch.current;
    const next = await client.getSettings();
    if (request !== epoch.current) return;
    setSettings(next);
    setForm(next.analysis ?? DEFAULTS);
    setHours(String(next.analysis?.interval_hours ?? 24));
    setKey('');
    setRemoveKey(false);
    setNeedsReload(false);
    setError(null);
  }, [client]);
  useEffect(() => {
    void load().catch(() => setError('Could not load the analysis settings. Select Reload settings to try again.'));
    return () => { epoch.current += 1; };
  }, [load]);
  const original = settings?.analysis ?? DEFAULTS;
  const managed = environmentManaged(settings);
  const validHours = /^\d+$/.test(hours) && Number(hours) <= 168;
  const changes: SettingsPatch = {};
  for (const field of ['enabled', 'provider', 'base_url', 'model'] as const) {
    if (form[field] !== original[field]) changes[PATCH_FIELDS[field]] = form[field];
  }
  if (validHours && Number(hours) !== original.interval_hours) changes.analysis_interval_hours = Number(hours);
  if (!managed.has('analysis_api_key') && (removeKey || key)) changes.analysis_api_key = removeKey ? '' : key;
  const dirty = Object.keys(changes).length > 0 || !validHours;
  const writable = isOwner && !busy && !needsReload;
  const save = async () => {
    if (fence.current || !writable || !validHours || !dirty || !settings?._meta?.desired_revision_id) return;
    fence.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await client.updateSettings({ expected_revision_id: settings._meta.desired_revision_id, ...changes });
      await load();
      setRefreshKey((value) => value + 1);
      setNotice('Settings saved.');
    } catch (caught) {
      setNeedsReload(true);
      setError(caught instanceof AdminAPIError && caught.status === 409
        ? 'Someone else changed the settings. Your edits are kept here; reload before saving again.'
        : failure(caught, 'The save could not be confirmed. Your edits are kept here; reload to check the saved settings.'));
    } finally { fence.current = false; setBusy(false); }
  };
  const test = async () => {
    if (fence.current || !writable || dirty || !original.configured) return;
    fence.current = true;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const result = await client.call<{ ok: boolean; message: string }>({ method: 'POST', path: '/analysis/test' });
      if (result.ok) setNotice(result.message);
      else setError(result.message);
    } catch (caught) { setError(failure(caught, 'The connection test failed. Check the saved provider, model and API key.')); }
    finally { fence.current = false; setBusy(false); }
  };
  return (
    <Box>
      <ScreenHeader title="AI analysis" subtitle="Optional reviews of this installation using your chosen AI service" />
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {notice && <Alert severity="success" sx={{ mb: 2 }}>{notice}</Alert>}
      <Paper variant="outlined" sx={{ p: 3, mb: 3, borderRadius: 2 }}>
        <Stack spacing={2}>
          <Typography variant="h6" component="h2">Analysis settings</Typography>
          <Typography variant="body2">When enabled, Faxbot sends an operational evidence summary to your chosen service. Fax documents and API keys are not included in that summary. Your service may charge for each analysis and connection test.</Typography>
          {!isOwner && <Alert severity="info">Only the owner of this installation can change these settings or run an analysis.</Alert>}
          {!settings ? <CircularProgress size={22} aria-label="Loading settings" /> : <>
            <FormControlLabel label="Enable AI analysis" control={<Switch checked={form.enabled} disabled={!writable}
              onChange={(_, enabled) => setForm((previous) => ({ ...previous, enabled }))} />} />
            <TextField select label="AI provider" value={form.provider} disabled={!writable}
              onChange={(event) => {
                const provider = event.target.value as AnalysisSettings['provider'];
                setForm((previous) => ({ ...previous, provider, base_url: ENDPOINTS[provider] }));
              }}>
              {Object.entries(PROVIDERS).map(([value, label]) => <MenuItem key={value} value={value}>{label}</MenuItem>)}
            </TextField>
            <TextField label="Service address" value={form.base_url} disabled={!writable}
              helperText="The service's API address. Use your provider's address or your compatible service's address."
              onChange={(event) => setForm((previous) => ({ ...previous, base_url: event.target.value }))} />
            <TextField label="Model" value={form.model} disabled={!writable}
              helperText="Enter a model available to your account."
              onChange={(event) => setForm((previous) => ({ ...previous, model: event.target.value }))} />
            {managed.has('analysis_api_key') ? <EnvSetField label="API key" /> : <>
              <TextField label="API key" type="password" autoComplete="new-password" value={key} disabled={!writable || removeKey}
                helperText={original.api_key ? 'A key is saved. Leave this blank to keep it, or enter a replacement.' : 'Enter the key for your AI service.'}
                onChange={(event) => setKey(event.target.value)} />
              {original.api_key && isOwner && <FormControlLabel label="Remove saved API key"
                control={<Checkbox checked={removeKey} disabled={!writable} onChange={(_, checked) => { setRemoveKey(checked); setKey(''); }} />} />}
            </>}
            <TextField label="Hours between analyses" type="number" value={hours} disabled={!writable}
              inputProps={{ min: 0, max: 168, step: 1 }} error={!validHours}
              helperText={validHours ? 'Use 0 for manual analysis only, or 1–168 hours for scheduled reviews.' : 'Enter a whole number from 0 to 168.'}
              onChange={(event) => setHours(event.target.value)} />
            {dirty && <Typography variant="body2" color="text.secondary">Save your changes before testing or running analysis.</Typography>}
          </>}
          <Box display="flex" gap={1} flexWrap="wrap">
            {isOwner && <>
              <Button variant="contained" onClick={() => void save()} disabled={!writable || !settings?._meta?.desired_revision_id || !dirty || !validHours}>Save settings</Button>
              <Button onClick={() => void test()} disabled={!writable || dirty || !original.configured}>Test connection</Button>
            </>}
            <Button disabled={busy} onClick={() => void load().catch(() => setError('Could not reload the settings. Try again.'))}>Reload settings</Button>
          </Box>
        </Stack>
      </Paper>
      <AnalysisCard client={client} canRun={isOwner} settingsPage refreshKey={refreshKey}
        actionsDisabled={busy || dirty || needsReload || !settings} />
    </Box>
  );
}
