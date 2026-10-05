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
  Science as ScienceIcon,
  Upload as UploadIcon,
  ExpandMore as ExpandMoreIcon,
  ExpandLess as ExpandLessIcon,
} from '@mui/icons-material';
import AdminAPIClient, { configurationWriteRejected } from '../api/client';
import { curatedDocsLink } from '../docsLinks';
import type { AdminConfig, ConfigurationWriteResult, PluginConfiguration, PluginConfigurationPatch, Settings } from '../api/types';
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
  const [saveResult, setSaveResult] = useState<ConfigurationWriteResult | null>(null);
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
        throw new Error('Plugin settings could not be loaded. Refresh to try again.');
      }
      const [listRes, active] = await Promise.all([
        client.listPlugins(),
        client.getConfig(),
      ]);
      const after = await client.getSettings();
      if (loadId !== listLoadId.current) return false;
      if (before._meta.desired_revision_id !== after._meta?.desired_revision_id
          || before._meta.active_revision_id !== after._meta?.active_revision_id
          || before._meta.generation !== after._meta?.generation) {
        throw new Error('Settings changed while plugins were loading. Refresh to see the current values.');
      }
      setItems(listRes.items || []);
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
    setSaveResult(null);
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
        throw new Error('Plugin settings could not be loaded. Reload to try again.');
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

  const saveMessage = (data: ConfigurationWriteResult) => data._meta.apply_state === 'pending_restart'
    ? `${data.changed ? 'Settings saved.' : 'Nothing changed.'} Restart Faxbot to apply pending changes.`
    : data.changed ? 'Settings saved.' : 'Nothing changed.';

  const mutationError = (e: any, conflict: string) => (e?.message || '').includes('409')
    ? conflict
    : configurationWriteRejected(e)
      ? `Settings were not saved (error ${e.status}). Reload to try again.`
      : 'Faxbot could not confirm the save. Reload to check the current values.';

  const handleSaveConfig = async (payload: PluginConfigurationPatch) => {
    if (!configPlugin || !configWritable.current || actionFence.current) throw new Error('Reload settings before saving.');
    const pluginId = configPlugin.id;
    const epoch = clientEpoch.current;
    actionFence.current = true;
    let writeResult: ConfigurationWriteResult;
    try {
      setSaving(pluginId);
      setError('');
      setNote('');
      setSaveResult(null);
      writeResult = await client.updatePluginConfig(pluginId, payload);
      if (epoch !== clientEpoch.current) return;
    } catch (e: any) {
      if (epoch !== clientEpoch.current) return;
      configWritable.current = false;
      setSettings(null);
      const message = mutationError(e, 'Someone else changed these settings. Your edits are kept here; reload to see the current values.');
      setError(message);
      setConfigError(message);
      actionFence.current = false;
      setSaving(null);
      throw new Error(message);
    }
    // The save succeeded. Later read errors must not reject onSave, which
    // would leave the dialog reporting a failed save.
    configWritable.current = false;
    const loadId = ++configLoadId.current;
    setConfigData(null);
    setConfigLoading(true);
    setConfigError('');
    setSettings(null);
    setActiveProviders(null);
    setSaveResult(writeResult);
    const saved = saveMessage(writeResult);
    setNote(saved);
    try {
      try {
        const desired = await client.getPluginConfig(pluginId, payload.role);
        if (!desired._meta?.desired_revision_id) throw new Error('Plugin settings could not be loaded.');
        if (loadId === configLoadId.current) {
          setConfigData(desired);
          configWritable.current = true;
        }
      } catch {
        if (epoch !== clientEpoch.current) return;
        if (loadId === configLoadId.current) setConfigError('Settings saved, but they could not be reloaded. Reload to keep editing.');
        setNote(saved);
      }
      if (epoch !== clientEpoch.current) return;
      if (!await load()) {
        if (epoch === clientEpoch.current) setNote(saved);
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
    let writeResult: ConfigurationWriteResult;
    try {
      setSaving(pluginId);
      setError('');
      setNote('');
      setSaveResult(null);
      writeResult = await client.updatePluginConfig(pluginId, {
        enabled: true, role: 'outbound', expected_revision_id: desiredRevision,
      });
      if (epoch !== clientEpoch.current) return;
    } catch (e: any) {
      if (epoch !== clientEpoch.current) return;
      setSettings(null);
      setError(mutationError(e, 'Someone else changed these settings. Refresh to see the current values.'));
      actionFence.current = false;
      setSaving(null);
      return;
    }
    configWritable.current = false;
    configLoadId.current += 1;
    setConfigData(null);
    setSettings(null);
    setActiveProviders(null);
    setSaveResult(writeResult);
    const saved = saveMessage(writeResult);
    setNote(saved);
    try {
      if (!await load() && epoch === clientEpoch.current) setNote(saved);
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
    const hay = `${p.id} ${p.name} ${p.description || ''}`.toLowerCase();
    return hay.includes(q);
  };
  
  const byCategory = (cat: string) => (items || []).filter(p => (p.categories || []).includes(cat)).filter(matches);
  
  const renderedClientEpoch = clientEpoch.current;
  const pendingCount = settings?._meta?.pending_fields?.length ?? 0;
  const showRestartNotice = settings?._meta?.apply_state === 'pending_restart'
    && !(note && saveResult?._meta.apply_state === 'pending_restart');

  return (
    <Box sx={{ p: { xs: 2, sm: 0 } }}>
      <Box sx={{ mb: 3 }}>
        <Typography variant="h4" component="h1" gutterBottom>
          Plugins
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Choose fax and storage providers and manage their settings.
        </Typography>
        {activeProviders && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            In use: {activeProviders.outbound} for outbound faxes, {activeProviders.storage} for storage.
          </Typography>
        )}
      </Box>

      <Stack spacing={3}>
        <Paper sx={{ p: 2, borderRadius: 2 }}>
          <TextField 
            size="medium" 
            fullWidth 
            label="Search plugins"
            placeholder="Search plugins…" 
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

        {showRestartNotice && (
          <Alert severity="warning" sx={{ borderRadius: 2 }}>
            {pendingCount > 0
              ? `Restart Faxbot to apply ${pendingCount} pending ${pendingCount === 1 ? 'change' : 'changes'}.`
              : 'Restart Faxbot to apply pending changes.'}
          </Alert>
        )}

        <Box><Button onClick={load} disabled={loading || saving !== null}>Refresh plugins</Button></Box>
        
        {note && (
          <Fade in>
            <Alert severity={saveResult?._meta.apply_state === 'pending_restart' ? 'warning' : 'success'} onClose={() => setNote('')} sx={{ borderRadius: 2 }}>
              {note}
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
              docsBase={docsBase}
              icon={<StorageIcon />}
            />
            
            {/* HTTP Manifest Tester */}
            <ResponsiveFormSection
              title="HTTP Manifest Tester (Preview)"
              subtitle="Check a provider manifest without sending; installing adds the provider but does not select it."
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
                      To send a test fax, install the provider, select it for outbound faxes, then open Send.{' '}
                      {activeConfigLoading ? 'Checking whether sending is on…'
                        : activeConfigError || !config || typeof config.fax_disabled !== 'boolean'
                          ? 'Sending status is unavailable; open Send to check it.'
                          : config.fax_disabled
                            ? 'Sending is off, so test faxes are held and never transmitted.'
                            : 'Sending is on, so faxes go out through the selected provider.'}
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
              subtitle="Paste a JSON array of manifests or Markdown with JSON code blocks; invalid entries are skipped."
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
  docsBase?: string;
  icon?: React.ReactNode;
}) {
  const joinCaps = (caps: string[]) => caps.join(', ');
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  
  return (
    <ResponsiveFormSection
      title={title}
      icon={icon}
    >
      <Grid container spacing={2}>
        {(items || []).map(p => {
          const desc = p.description;
          const learn = p.source === 'manifest'
            ? p.learn_more
            : curatedDocsLink(p.learn_more, docsBase);
          
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
                      label={p.enabled ? 'Selected' : 'Not selected'}
                      color={p.enabled ? 'success' : 'default'}
                      sx={{ borderRadius: 1 }}
                    />
                  </Box>

                  {activeProvider === p.id && <Chip size="small" label="In use" variant="outlined" sx={{ mb: 1 }} />}
                  
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
                      <Tooltip title="Edit settings for this plugin">
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
                      <Tooltip title="Use this provider for outbound faxes">
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
