import { useEffect, useRef, useState } from 'react';
import { 
  Box, 
  Grid, 
  Card, 
  CardContent, 
  CardActions, 
  Typography, 
  Button, 
  Chip, 
  Link as MLink, 
  Tooltip, 
  Alert, 
  TextField,
  useTheme,
  useMediaQuery,
  Stack,
  Paper,
  Fade,
  CircularProgress,
  Collapse,
} from '@mui/material';
import { 
  Extension, 
  Cloud, 
  Storage as StorageIcon, 
  Phone, 
  WarningAmber,
  Search as SearchIcon,
  CloudDownload as CloudDownloadIcon,
  Science as ScienceIcon,
  Upload as UploadIcon,
  ExpandMore as ExpandMoreIcon,
  ExpandLess as ExpandLessIcon,
} from '@mui/icons-material';
import AdminAPIClient, { configurationWriteRejected } from '../api/client';
import { curatedDocsLink } from '../docsLinks';
import type { AdminConfig, ConfigurationWriteReceipt, PluginConfiguration, PluginConfigurationPatch, Settings } from '../api/types';
import type { AdminDestination } from '../navigation';
import PluginConfigDialog from './PluginConfigDialog';
import { ResponsiveFormSection } from './common/ResponsiveFormFields';

type Props = {
  client: AdminAPIClient;
  config: AdminConfig | null;
  configLoading: boolean;
  configError: string | null;
  onNavigate: (destination: AdminDestination) => void;
};

type PluginItem = {
  id: string;
  name: string;
  version: string;
  categories: string[];
  capabilities: string[];
  enabled?: boolean;
  configurable?: boolean;
  description?: string;
  learn_more?: string;
  source?: string;
};

const iconFor = (cat: string) => {
  switch ((cat || '').toLowerCase()) {
    case 'outbound': return <Phone fontSize="small" />;
    case 'storage': return <StorageIcon fontSize="small" />;
    default: return <Extension fontSize="small" />;
  }
};

const EXAMPLE_MANIFEST = `{
  "id": "example",
  "allowed_domains": ["api.example.com"],
  "actions": {
    "send_fax": {
      "method": "POST",
      "url": "https://api.example.com/fax",
      "body": {
        "kind": "json",
        "template": "{\\"to\\":\\"{{ to }}\\", \\"file_url\\":\\"{{ file_url }}\\"}"
      },
      "response": {
        "job_id": "data.id",
        "status": "data.status"
      }
    }
  }
}`;

const BULK_IMPORT_PLACEHOLDER = `[ { "id": "provider1", ... }, { ... } ] or markdown with json code blocks`;

export default function Plugins({ client, config, configLoading: activeConfigLoading, configError: activeConfigError, onNavigate }: Props) {
  const docsBase = config?.branding?.docs_base;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');
  const [items, setItems] = useState<PluginItem[]>([]);
  const [registry, setRegistry] = useState<PluginItem[]>([]);
  const [saving, setSaving] = useState<string | null>(null);
  const [note, setNote] = useState<string>('');
  const [configOpen, setConfigOpen] = useState(false);
  const [configPlugin, setConfigPlugin] = useState<PluginItem | null>(null);
  const [configData, setConfigData] = useState<PluginConfiguration | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState('');
  const configLoadId = useRef(0);
  const configWritable = useRef(false);
  const actionFence = useRef(false);
  const listLoadId = useRef(0);
  const clientEpoch = useRef(0);
  const [saveReceipt, setSaveReceipt] = useState<ConfigurationWriteReceipt | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [activeProviders, setActiveProviders] = useState<{ outbound: string; storage: string } | null>(null);
  const [query, setQuery] = useState('');
  const [manifestJson, setManifestJson] = useState<string>('');
  const [manifestResult, setManifestResult] = useState<any | null>(null);
  const [bulkText, setBulkText] = useState<string>('');
  const [bulkImportRes, setBulkImportRes] = useState<any | null>(null);
  const [manifestExpanded, setManifestExpanded] = useState(false);
  const [bulkExpanded, setBulkExpanded] = useState(false);

  const theme = useTheme();
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

  const load = async () => {
    const loadId = ++listLoadId.current;
    try {
      setLoading(true);
      setError('');
      setSettings(null);
      setActiveProviders(null);
      const before = await client.getSettings();
      if (!before._meta?.desired_revision_id) {
        throw new Error('Plugin selection is unavailable until a canonical revision loads.');
      }
      const [listRes, regRes, active] = await Promise.all([
        client.listPlugins(),
        client.getPluginRegistry(),
        client.getConfig(),
      ]);
      const after = await client.getSettings();
      if (loadId !== listLoadId.current) return false;
      if (before._meta.desired_revision_id !== after._meta?.desired_revision_id
          || before._meta.active_revision_id !== after._meta?.active_revision_id
          || before._meta.generation !== after._meta?.generation) {
        throw new Error('Settings changed while plugins loaded. Refresh plugins to review the current selection.');
      }
      setItems(listRes.items || []);
      setRegistry(regRes.items || []);
      setSettings(after);
      setActiveProviders({
        outbound: active.hybrid?.outbound ?? active.backend,
        storage: active.storage?.backend,
      });
      return true;
    } catch (e: any) {
      if (loadId === listLoadId.current) setError(e?.message || 'Failed to load plugins');
      return false;
    } finally {
      if (loadId === listLoadId.current) setLoading(false);
    }
  };

  useEffect(() => {
    clientEpoch.current += 1;
    actionFence.current = false;
    configWritable.current = false;
    setSaving(null);
    setNote('');
    setSaveReceipt(null);
    setConfigOpen(false);
    setConfigPlugin(null);
    setConfigData(null);
    setConfigError('');
    setConfigLoading(false);
    void load();
    return () => {
      clientEpoch.current += 1;
      listLoadId.current += 1;
      configLoadId.current += 1;
      configWritable.current = false;
    };
  }, [client]);

  const handleConfigure = async (plugin: PluginItem) => {
    const loadId = ++configLoadId.current;
    configWritable.current = false;
    setConfigPlugin(plugin);
    setConfigData(null);
    setConfigError('');
    setConfigLoading(true);
    setConfigOpen(true);
    try {
      const role = plugin.categories.includes('storage') ? 'storage' : 'outbound';
      const cfg = await client.getPluginConfig(plugin.id, role);
      if (!cfg._meta?.desired_revision_id) {
        throw new Error('The server did not supply a configuration revision. Reload before editing.');
      }
      if (loadId === configLoadId.current) {
        setConfigData(cfg);
        configWritable.current = true;
      }
    } catch (e: any) {
      if (loadId === configLoadId.current) setConfigError(e?.message || 'Failed to load plugin config');
    } finally {
      if (loadId === configLoadId.current) setConfigLoading(false);
    }
  };

  const saveMessage = (data: ConfigurationWriteReceipt) => data._meta.apply_state === 'pending_restart'
    ? `${data.changed ? 'Desired plugin settings saved durably.' : 'Desired plugin settings are unchanged.'} Pending changes require every API worker to stop and the installation to restart; active settings remain in effect for those fields.`
    : data.changed ? 'Plugin settings saved durably and active.' : 'Plugin settings are unchanged and active.';

  const mutationError = (e: any, fallback: string) => (e?.message || '').includes('409')
    ? 'Plugin settings changed since this revision loaded. Your draft is retained. Reload explicitly to review the current desired revision before saving again.'
    : `${configurationWriteRejected(e) ? 'Save was rejected. Your draft is retained.' : 'Save was not confirmed.'} ${e?.message || fallback} Reload to check the current configuration before saving again.`;

  const handleSaveConfig = async (payload: PluginConfigurationPatch) => {
    if (!configPlugin || !configWritable.current || actionFence.current) throw new Error('Reload a plugin before saving. Your draft is retained until Reload.');
    const pluginId = configPlugin.id;
    const epoch = clientEpoch.current;
    actionFence.current = true;
    let receipt: ConfigurationWriteReceipt;
    try {
      setSaving(pluginId);
      setError('');
      setNote('');
      setSaveReceipt(null);
      receipt = await client.updatePluginConfig(pluginId, payload);
      if (epoch !== clientEpoch.current) return;
    } catch (e: any) {
      if (epoch !== clientEpoch.current) return;
      configWritable.current = false;
      setSettings(null);
      const message = mutationError(e, 'Failed to save plugin config');
      setError(message);
      setConfigError(message);
      actionFence.current = false;
      setSaving(null);
      throw new Error(message);
    }
    // The receipt confirms the mutation. Subsequent read errors must not reject
    // onSave, which would leave the dialog reporting a failed save.
    configWritable.current = false;
    const loadId = ++configLoadId.current;
    setConfigData(null);
    setConfigLoading(true);
    setConfigError('');
    setSettings(null);
    setActiveProviders(null);
    setSaveReceipt(receipt);
    const saved = saveMessage(receipt);
    setNote(saved);
    try {
      try {
        const desired = await client.getPluginConfig(pluginId, payload.role);
        if (!desired._meta?.desired_revision_id) throw new Error('No canonical desired revision was returned.');
        if (loadId === configLoadId.current) {
          setConfigData(desired);
          configWritable.current = true;
        }
      } catch {
        if (epoch !== clientEpoch.current) return;
        if (loadId === configLoadId.current) setConfigError('Save confirmed; the plugin settings view could not be reloaded. Reload explicitly, or sign in again, before another save.');
        setNote(`${saved} Save confirmed; the plugin settings view could not be reloaded. Reload explicitly, or sign in again, before another save.`);
      }
      if (epoch !== clientEpoch.current) return;
      if (!await load()) {
        if (epoch === clientEpoch.current) setNote(`${saved} Save confirmed; the provider list could not be reloaded. Refresh plugins explicitly, or sign in again, before another selection.`);
      }
    } finally {
      if (epoch === clientEpoch.current) {
        if (loadId === configLoadId.current) setConfigLoading(false);
        actionFence.current = false;
        setSaving(null);
      }
    }
  };

  const handleMakeActiveOutbound = async (pluginId: string) => {
    const desiredRevision = settings?._meta?.desired_revision_id;
    if (actionFence.current || !desiredRevision) return;
    const epoch = clientEpoch.current;
    actionFence.current = true;
    let receipt: ConfigurationWriteReceipt;
    try {
      setSaving(pluginId);
      setError('');
      setNote('');
      setSaveReceipt(null);
      receipt = await client.updatePluginConfig(pluginId, {
        enabled: true, role: 'outbound', expected_revision_id: desiredRevision,
      });
      if (epoch !== clientEpoch.current) return;
    } catch (e: any) {
      if (epoch !== clientEpoch.current) return;
      setSettings(null);
      setError(mutationError(e, 'Failed to save plugin config'));
      actionFence.current = false;
      setSaving(null);
      return;
    }
    configWritable.current = false;
    configLoadId.current += 1;
    setConfigData(null);
    setSettings(null);
    setActiveProviders(null);
    setSaveReceipt(receipt);
    const saved = saveMessage(receipt);
    setNote(saved);
    try {
      if (!await load() && epoch === clientEpoch.current) setNote(`${saved} Save confirmed; the provider list could not be reloaded. Refresh plugins explicitly, or sign in again, before another selection.`);
    } finally {
      if (epoch === clientEpoch.current) {
        actionFence.current = false;
        setSaving(null);
      }
    }
  };

  const matches = (p: PluginItem) => {
    if (!query) return true;
    const q = query.toLowerCase();
    const inReg = p.source === 'manifest' ? undefined : (registry || []).find(r => r.id === p.id);
    const hay = `${p.id} ${p.name} ${p.description || inReg?.description || ''}`.toLowerCase();
    return hay.includes(q);
  };
  
  const byCategory = (cat: string) => (items || []).filter(p => (p.categories || []).includes(cat)).filter(matches);
  
  const registryOnly = () => {
    const installed = new Set((items || []).map(i => i.id));
    return (registry || []).filter(r => !installed.has(r.id) && matches(r as any));
  };
  const renderedClientEpoch = clientEpoch.current;

  return (
    <Box sx={{ p: { xs: 2, sm: 0 } }}>
      <Box sx={{ mb: 3 }}>
        <Typography variant="h4" component="h1" gutterBottom>
          Plugins
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Manage the desired provider selection and settings. Changes save durably in the installation configuration database.
        </Typography>
      </Box>

      <Stack spacing={3}>
        <Paper sx={{ p: 2, borderRadius: 2 }}>
          <TextField 
            size="medium" 
            fullWidth 
            label="Search plugins"
            placeholder="Search curated plugins…" 
            value={query} 
            onChange={(e) => setQuery(e.target.value)}
            InputProps={{
              startAdornment: <SearchIcon sx={{ mr: 1, color: 'text.secondary' }} />,
            }}
            sx={{
              '& .MuiOutlinedInput-root': {
                borderRadius: 2,
              }
            }}
          />
        </Paper>

        <Alert severity={settings?._meta?.apply_state === 'pending_restart' ? 'warning' : 'info'} sx={{ borderRadius: 2 }}>
          {settings?._meta?.apply_state === 'pending_restart'
            ? 'A desired revision is pending restart. Active behavior continues until every worker stops and the installation starts again.'
            : 'Hot changes activate immediately. Changes that replace runtime resources remain pending until a full installation restart.'}
          {activeProviders && <Typography variant="body2" sx={{ mt: 1 }}>
            Active outbound setting: {activeProviders.outbound}. Active storage setting: {activeProviders.storage}.
          </Typography>}
          {settings?._meta && <Typography variant="caption" display="block" sx={{ mt: 1 }}>
            Desired revision: {settings._meta.desired_revision_id}. Active revision: {settings._meta.active_revision_id}.
          </Typography>}
        </Alert>

        <Box><Button onClick={load} disabled={loading || saving !== null}>Refresh plugins</Button></Box>
        
        {note && (
          <Fade in>
            <Alert severity={saveReceipt?._meta.apply_state === 'pending_restart' ? 'warning' : 'success'} onClose={() => setNote('')} sx={{ borderRadius: 2 }}>
              {note}
              {saveReceipt && <Typography variant="caption" display="block" sx={{ mt: 1 }}>
                Confirmed desired revision: {saveReceipt._meta.desired_revision_id}. Active revision: {saveReceipt._meta.active_revision_id}. Generation: {saveReceipt._meta.generation}.
              </Typography>}
            </Alert>
          </Fade>
        )}
        
        {error && (
          <Fade in>
            <Alert severity="error" onClose={() => setError('')} sx={{ borderRadius: 2 }}>
              {error}
            </Alert>
          </Fade>
        )}

        {loading ? (
          <Box sx={{ textAlign: 'center', py: 4 }}>
            <CircularProgress />
            <Typography sx={{ mt: 2 }}>Loading plugins…</Typography>
          </Box>
        ) : (
          <>
            <Section 
              title="Outbound Providers" 
              items={byCategory('outbound')}
              saving={saving} 
              disabled={!settings?._meta?.desired_revision_id || saving !== null}
              activeProvider={activeProviders?.outbound}
              onActivate={handleMakeActiveOutbound} 
              onConfigure={handleConfigure} 
              registry={registry} 
              docsBase={docsBase}
              icon={<Phone />}
            />
            
            <Section 
              title="Storage Providers" 
              items={byCategory('storage')} 
              saving={saving} 
              disabled={!settings?._meta?.desired_revision_id || saving !== null}
              activeProvider={activeProviders?.storage}
              onActivate={undefined} 
              onConfigure={handleConfigure} 
              registry={registry} 
              docsBase={docsBase}
              icon={<StorageIcon />}
            />
            
            <Discover 
              title="Discover (Curated Registry)" 
              items={registryOnly()} 
              docsBase={docsBase}
              icon={<CloudDownloadIcon />}
            />
            
            {/* HTTP Manifest Tester */}
            <ResponsiveFormSection
              title="HTTP Manifest Tester (Preview)"
              subtitle="Validate the draft without sending. Installing saves the provider manifest and does not activate it."
              icon={<ScienceIcon />}
            >
              <Box>
                <Button
                  size="small"
                  onClick={() => setManifestExpanded(!manifestExpanded)}
                  startIcon={manifestExpanded ? <ExpandLessIcon /> : <ExpandMoreIcon />}
                  sx={{ mb: 2 }}
                >
                  {manifestExpanded ? 'Hide' : 'Show'} Manifest Tester
                </Button>
                <Collapse in={manifestExpanded}>
                  <Stack spacing={2}>
                    <TextField 
                      label="Manifest JSON" 
                      value={manifestJson} 
                      onChange={(e) => setManifestJson(e.target.value)} 
                      fullWidth 
                      multiline 
                      minRows={isSmallMobile ? 6 : 8}
                      placeholder={EXAMPLE_MANIFEST}
                      sx={{
                        '& .MuiOutlinedInput-root': {
                          borderRadius: 2,
                          fontFamily: 'monospace',
                          fontSize: '0.875rem',
                        }
                      }}
                    />
                    
                    <Stack direction="row" spacing={1} flexWrap="wrap">
                      <Button 
                        size="medium" 
                        variant="outlined" 
                        onClick={async () => {
                          try {
                            setError(''); setNote(''); setManifestResult(null);
                            const parsed = JSON.parse(manifestJson || '{}');
                            const r = await client.validateHttpManifest({ manifest: parsed, render_only: true });
                            setManifestResult(r);
                          } catch (e: any) {
                            setError(e?.message || 'Validate failed');
                          }
                        }}
                        sx={{ borderRadius: 2 }}
                      >
                        Validate
                      </Button>
                      <Button 
                        size="medium" 
                        variant="contained" 
                        onClick={async () => {
                          try {
                            setError(''); setNote('');
                            const parsed = JSON.parse(manifestJson || '{}');
                            const r = await client.installHttpManifest({ manifest: parsed });
                            setNote(`Installed manifest ${r.id}`);
                            await load();
                          } catch (e: any) {
                            setError(e?.message || 'Install failed');
                          }
                        }}
                        sx={{ borderRadius: 2 }}
                      >
                        Install
                      </Button>
                      <Button
                        size="medium"
                        variant="outlined"
                        onClick={() => onNavigate('send')}
                        sx={{ borderRadius: 2 }}
                      >
                        Open Send
                      </Button>
                    </Stack>

                    <Alert severity="info">
                      To test a fax, install and configure the provider, select it as active outbound in Settings,
                      then open Send to upload a document and create a tracked fax. Send uses the active provider,
                      not this draft preview.
                      <Typography variant="body2" sx={{ mt: 1 }}>
                        {activeConfigLoading ? 'Checking the active delivery mode…'
                          : activeConfigError || !config || typeof config.fax_disabled !== 'boolean'
                            ? 'Active delivery mode could not be loaded. Open Send to refresh it; submission stays unavailable until active settings load.'
                            : config.fax_disabled
                              ? 'Current active mode permanently holds new test faxes without transmission. Enabling sending later does not release held jobs.'
                              : 'Current active mode permits real fax transmission through the selected active outbound provider.'}
                      </Typography>
                    </Alert>
                    
                    {manifestResult && (
                      <Paper sx={{ p: 2, borderRadius: 2, bgcolor: 'background.paper' }}>
                        <Typography variant="subtitle2" fontWeight={600} gutterBottom>
                          Result
                        </Typography>
                        <pre style={{ 
                          margin: 0, 
                          fontSize: '0.75rem',
                          overflow: 'auto',
                          maxHeight: '300px'
                        }}>
                          {JSON.stringify(manifestResult, null, 2)}
                        </pre>
                      </Paper>
                    )}
                  </Stack>
                </Collapse>
              </Box>
            </ResponsiveFormSection>
            
            {/* Bulk Import Providers */}
            <ResponsiveFormSection
              title="Bulk Import Providers (Preview)"
              subtitle="Paste either a JSON array of manifests or scraped Markdown containing JSON code blocks. We'll import valid manifests and ignore the rest."
              icon={<UploadIcon />}
            >
              <Box>
                <Button
                  size="small"
                  onClick={() => setBulkExpanded(!bulkExpanded)}
                  startIcon={bulkExpanded ? <ExpandLessIcon /> : <ExpandMoreIcon />}
                  sx={{ mb: 2 }}
                >
                  {bulkExpanded ? 'Hide' : 'Show'} Bulk Import
                </Button>
                <Collapse in={bulkExpanded}>
                  <Stack spacing={2}>
                    <TextField 
                      label="Manifests JSON or Markdown" 
                      value={bulkText} 
                      onChange={(e) => setBulkText(e.target.value)} 
                      fullWidth 
                      multiline 
                      minRows={isSmallMobile ? 6 : 8}
                      placeholder={BULK_IMPORT_PLACEHOLDER}
                      sx={{
                        '& .MuiOutlinedInput-root': {
                          borderRadius: 2,
                          fontFamily: 'monospace',
                          fontSize: '0.875rem',
                        }
                      }}
                    />
                    
                    <Stack direction="row" spacing={1} flexWrap="wrap">
                      <Button 
                        size="medium" 
                        variant="contained" 
                        onClick={async () => {
                          try {
                            setError(''); setNote(''); setBulkImportRes(null);
                            let payload: any = {};
                            try {
                              const parsed = JSON.parse(bulkText);
                              if (Array.isArray(parsed)) payload.items = parsed; 
                              else if (parsed && Array.isArray(parsed.items)) payload.items = parsed.items;
                            } catch {
                              payload.markdown = bulkText;
                            }
                            const res = await client.importHttpManifests(payload);
                            setBulkImportRes(res);
                            await load();
                          } catch (e: any) {
                            setError(e?.message || 'Import failed');
                          }
                        }}
                        sx={{ borderRadius: 2 }}
                      >
                        Import
                      </Button>
                      <Button 
                        size="medium" 
                        variant="outlined"
                        onClick={async () => {
                          try {
                            setError(''); setNote(''); setBulkImportRes(null);
                            const res = await client.importHttpManifests({ source: 'repo_scrape' });
                            setBulkImportRes(res);
                            await load();
                          } catch (e: any) {
                            setError(e?.message || 'Import from repo failed');
                          }
                        }}
                        sx={{ borderRadius: 2 }}
                      >
                        Import from repo scrape
                      </Button>
                    </Stack>
                    
                    {bulkImportRes && (
                      <Paper sx={{ p: 2, borderRadius: 2, bgcolor: 'background.paper' }}>
                        <Alert severity={(bulkImportRes.imported?.length || 0) === 0
                          ? (bulkImportRes.errors?.length || 0) > 0 ? 'error' : 'warning'
                          : (bulkImportRes.errors?.length || 0) > 0 ? 'warning' : 'success'} sx={{ mb: 2 }}>
                          Imported {bulkImportRes.imported?.length || 0} provider(s). {bulkImportRes.errors?.length || 0} failed.
                        </Alert>
                        <Typography variant="subtitle2" fontWeight={600} gutterBottom>
                          Import Summary
                        </Typography>
                        <pre style={{ 
                          margin: 0, 
                          fontSize: '0.75rem',
                          overflow: 'auto',
                          maxHeight: '300px'
                        }}>
                          {JSON.stringify(bulkImportRes, null, 2)}
                        </pre>
                      </Paper>
                    )}
                  </Stack>
                </Collapse>
              </Box>
            </ResponsiveFormSection>

            <PluginConfigDialog
              open={configOpen}
              plugin={configPlugin}
              initialConfig={configData}
              loading={configLoading}
              loadError={configError}
              onClose={() => {
                if (renderedClientEpoch !== clientEpoch.current) return;
                configLoadId.current += 1;
                configWritable.current = false;
                setConfigOpen(false);
                setConfigLoading(false);
              }}
              onReload={() => { if (configPlugin) handleConfigure(configPlugin); }}
              onSave={handleSaveConfig}
            />

          </>
        )}
      </Stack>
    </Box>
  );
}

function Section({ 
  title, 
  items, 
  saving, 
  disabled,
  activeProvider,
  onActivate, 
  onConfigure, 
  registry,
  docsBase,
  icon 
}: { 
  title: string; 
  items: PluginItem[]; 
  saving: string | null; 
  disabled: boolean;
  activeProvider?: string;
  onActivate?: (id: string) => void; 
  onConfigure?: (p: PluginItem) => void; 
  registry: PluginItem[];
  docsBase?: string;
  icon?: React.ReactNode;
}) {
  const joinCaps = (caps: string[]) => caps.join(', ');
  const regIndex = new Map((registry || []).map(r => [r.id, r] as const));
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  
  return (
    <ResponsiveFormSection
      title={title}
      icon={icon}
    >
      <Grid container spacing={2}>
        {(items || []).map(p => {
          const reg = p.source === 'manifest' ? undefined : regIndex.get(p.id);
          const desc = p.description || reg?.description;
          const learn = p.source === 'manifest'
            ? p.learn_more
            : curatedDocsLink(p.learn_more || reg?.learn_more, docsBase);
          
          return (
            <Grid item xs={12} sm={6} lg={4} key={p.id}>
              <Card 
                variant="outlined"
                sx={{
                  height: '100%',
                  display: 'flex',
                  flexDirection: 'column',
                  borderRadius: 2,
                  transition: 'all 0.3s ease',
                  '&:hover': {
                    transform: isMobile ? undefined : 'translateY(-2px)',
                    boxShadow: theme.palette.mode === 'dark'
                      ? '0 4px 12px rgba(0,0,0,0.4)'
                      : '0 4px 12px rgba(0,0,0,0.1)',
                  }
                }}
              >
                <CardContent sx={{ flexGrow: 1 }}>
                  <Box display="flex" alignItems="center" justifyContent="space-between" mb={1}>
                    <Box display="flex" alignItems="center">
                      <Cloud fontSize="small" sx={{ mr: 1, color: 'primary.main' }} />
                      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
                        {p.name}
                      </Typography>
                    </Box>
                    <Chip 
                      size="small" 
                      label={p.enabled ? 'Desired selection' : 'Not selected'}
                      color={p.enabled ? 'success' : 'default'}
                      sx={{ borderRadius: 1 }}
                    />
                  </Box>

                  {activeProvider === p.id && <Chip size="small" label="Active configuration" variant="outlined" sx={{ mb: 1 }} />}
                  
                  <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                    {desc || 'No description available.'}
                  </Typography>
                  
                  <Stack spacing={1}>
                    <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
                      {(p.categories || []).map(cat => (
                        <Chip 
                          key={cat} 
                          size="small" 
                          variant="outlined" 
                          sx={{ borderRadius: 1 }} 
                          icon={iconFor(cat)} 
                          label={cat} 
                        />
                      ))}
                    </Box>
                    
                    <Typography variant="caption" color="text.secondary">
                      Capabilities: {joinCaps(p.capabilities || []) || '—'}
                    </Typography>
                    
                    {learn && (
                      <MLink href={learn} target="_blank" rel="noreferrer" sx={{ fontSize: '0.875rem' }}>
                        Learn more
                      </MLink>
                    )}
                  </Stack>
                </CardContent>
                
                <CardActions sx={{ p: 2, pt: 0 }}>
                  <Stack direction="row" spacing={1} width="100%">
                    {onConfigure && (
                      <Tooltip title="Edit desired settings for this plugin">
                        <Button 
                          size="small" 
                          disabled={disabled}
                          onClick={() => onConfigure(p)}
                          sx={{ borderRadius: 1 }}
                        >
                          Configure
                        </Button>
                      </Tooltip>
                    )}
                    {onActivate ? (
                      <Tooltip title="Select this outbound provider in the desired revision">
                        <span style={{ marginLeft: 'auto' }}>
                          <Button 
                            size="small" 
                            variant="contained" 
                            disabled={disabled}
                            onClick={() => onActivate(p.id)}
                            startIcon={saving === p.id ? <CircularProgress size={16} /> : undefined}
                            sx={{ borderRadius: 1 }}
                          >
                            {saving === p.id ? 'Saving…' : 'Select outbound'}
                          </Button>
                        </span>
                      </Tooltip>
                    ) : (
                      <Tooltip title="Storage provider selection is controlled by server settings">
                        <span style={{ marginLeft: 'auto' }}>
                          <Button size="small" disabled sx={{ borderRadius: 1 }}>
                            Managed by server
                          </Button>
                        </span>
                      </Tooltip>
                    )}
                  </Stack>
                </CardActions>
              </Card>
            </Grid>
          );
        })}
        {(!items || items.length === 0) && (
          <Grid item xs={12}>
            <Alert icon={<WarningAmber />} severity="warning" sx={{ borderRadius: 2 }}>
              No plugins discovered.
            </Alert>
          </Grid>
        )}
      </Grid>
    </ResponsiveFormSection>
  );
}

function Discover({ title, items, icon, docsBase }: { title: string; items: any[]; icon?: React.ReactNode; docsBase?: string }) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  
  return (
    <ResponsiveFormSection
      title={title}
      icon={icon}
    >
      {(!items || items.length === 0) ? (
        <Alert severity="info" sx={{ borderRadius: 2 }}>
          No matches found in the curated registry.
        </Alert>
      ) : (
        <Grid container spacing={2}>
          {items.map((r) => (
            <Grid item xs={12} sm={6} lg={4} key={r.id}>
              <Card 
                variant="outlined"
                sx={{
                  height: '100%',
                  display: 'flex',
                  flexDirection: 'column',
                  borderRadius: 2,
                  transition: 'all 0.3s ease',
                  '&:hover': {
                    transform: isMobile ? undefined : 'translateY(-2px)',
                    boxShadow: theme.palette.mode === 'dark'
                      ? '0 4px 12px rgba(0,0,0,0.4)'
                      : '0 4px 12px rgba(0,0,0,0.1)',
                  }
                }}
              >
                <CardContent sx={{ flexGrow: 1 }}>
                  <Box display="flex" alignItems="center" justifyContent="space-between" mb={1}>
                    <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
                      {r.name}
                    </Typography>
                    <Chip 
                      size="small" 
                      label={r.version || '1.x'}
                      sx={{ borderRadius: 1 }}
                    />
                  </Box>
                  
                  <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                    {r.description || 'No description.'}
                  </Typography>
                  
                  <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
                    {(r.categories || []).map((cat: string) => (
                      <Chip 
                        key={cat} 
                        size="small" 
                        variant="outlined" 
                        sx={{ borderRadius: 1 }} 
                        label={cat} 
                      />
                    ))}
                  </Box>
                </CardContent>
                
                <CardActions sx={{ p: 2, pt: 0 }}>
                  {r.learn_more ? (
                    <MLink href={curatedDocsLink(r.learn_more, docsBase)} target="_blank" rel="noreferrer" sx={{ fontSize: '0.875rem' }}>
                      Learn more
                    </MLink>
                  ) : (
                    <Tooltip title="Remote install is disabled by default for security.">
                      <span>
                        <Button size="small" disabled sx={{ borderRadius: 1 }}>
                          Install Disabled
                        </Button>
                      </span>
                    </Tooltip>
                  )}
                </CardActions>
              </Card>
            </Grid>
          ))}
        </Grid>
      )}
    </ResponsiveFormSection>
  );
}
