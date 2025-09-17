import { useEffect, useState } from 'react';
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
import AdminAPIClient from '../api/client';
import PluginConfigDialog from './PluginConfigDialog';
import { ResponsiveFormSection, ResponsiveTextField } from './common/ResponsiveFormFields';

type Props = { client: AdminAPIClient };

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

export default function Plugins({ client }: Props) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');
  const [items, setItems] = useState<PluginItem[]>([]);
  const [registry, setRegistry] = useState<PluginItem[]>([]);
  const [saving, setSaving] = useState<string | null>(null);
  const [note, setNote] = useState<string>('');
  const [configOpen, setConfigOpen] = useState(false);
  const [configPlugin, setConfigPlugin] = useState<PluginItem | null>(null);
  const [configData, setConfigData] = useState<{ enabled?: boolean; settings?: any } | null>(null);
  const [query, setQuery] = useState('');
  const [manifestJson, setManifestJson] = useState<string>('');
  const [manifestTo, setManifestTo] = useState<string>('+15551234567');
  const [manifestFileUrl, setManifestFileUrl] = useState<string>('');
  const [manifestResult, setManifestResult] = useState<any | null>(null);
  const [bulkText, setBulkText] = useState<string>('');
  const [bulkImportRes, setBulkImportRes] = useState<any | null>(null);
  const [manifestExpanded, setManifestExpanded] = useState(false);
  const [bulkExpanded, setBulkExpanded] = useState(false);

  const theme = useTheme();
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

  const load = async () => {
    try {
      setLoading(true);
      setError('');
      const [listRes, regRes] = await Promise.all([
        client.listPlugins().catch(() => ({ items: [] })),
        client.getPluginRegistry().catch(() => ({ items: [] })),
      ]);
      setItems(listRes.items || []);
      setRegistry(regRes.items || []);
    } catch (e: any) {
      setError(e?.message || 'Failed to load plugins');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleConfigure = async (plugin: PluginItem) => {
    try {
      setError('');
      setConfigPlugin(plugin);
      try {
        const cfg = await client.getPluginConfig(plugin.id);
        setConfigData(cfg || { enabled: true, settings: {} });
      } catch {
        setConfigData({ enabled: true, settings: {} });
      }
      setConfigOpen(true);
    } catch (e: any) {
      setError(e?.message || 'Failed to load plugin config');
    }
  };

  const handleSaveConfig = async (payload: { enabled?: boolean; settings?: any }) => {
    if (!configPlugin) return;
    try {
      setSaving(configPlugin.id);
      await client.updatePluginConfig(configPlugin.id, payload);
      setNote('Plugin configuration saved to config file');
      await load();
    } catch (e: any) {
      setError(e?.message || 'Failed to save plugin config');
    } finally {
      setSaving(null);
    }
  };

  const handleMakeActiveOutbound = async (pluginId: string) => {
    try {
      setSaving(pluginId);
      await client.updatePluginConfig(pluginId, { enabled: true });
      setNote('Saved to config file. Apply changes by restarting with the desired env or adding an explicit apply step later.');
      await load();
    } catch (e: any) {
      setError(e?.message || 'Failed to save plugin config');
    } finally {
      setSaving(null);
    }
  };

  const matches = (p: PluginItem) => {
    if (!query) return true;
    const q = query.toLowerCase();
    const inReg = (registry || []).find(r => r.id === p.id);
    const hay = `${p.id} ${p.name} ${inReg?.description || ''}`.toLowerCase();
    return hay.includes(q);
  };
  
  const byCategory = (cat: string) => (items || []).filter(p => (p.categories || []).includes(cat)).filter(matches);
  
  const ensureDocumo = (arr: PluginItem[]) => {
    const has = arr.some(p => p.id === 'documo');
    if (!has) {
      arr = arr.concat([{ 
        id: 'documo', 
        name: 'Documo mFax', 
        version: '1.0.0', 
        categories: ['outbound'], 
        capabilities: ['send','get_status'], 
        description: 'Direct upload (preview)' 
      } as any]);
    }
    return arr;
  };
  
  const registryOnly = () => {
    const installed = new Set((items || []).map(i => i.id));
    return (registry || []).filter(r => !installed.has(r.id) && matches(r as any));
  };

  return (
    <Box sx={{ p: { xs: 2, sm: 0 } }}>
      <Box sx={{ mb: 3 }}>
        <Typography variant="h4" component="h1" gutterBottom>
          Plugins
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Manage provider plugins. This preview lists installed providers; updates persist to the config file only. No live apply yet.
        </Typography>
      </Box>

      <Stack spacing={3}>
        <Paper sx={{ p: 2, borderRadius: 2 }}>
          <TextField 
            size="medium" 
            fullWidth 
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

        <Alert severity="info" sx={{ borderRadius: 2 }}>
          Plugin changes are feature‑gated and safe to explore. Outbound provider remains controlled by env (<code>FAX_BACKEND</code>) until an explicit apply flow is added.
        </Alert>
        
        {note && (
          <Fade in>
            <Alert severity="success" onClose={() => setNote('')} sx={{ borderRadius: 2 }}>
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
              items={ensureDocumo(byCategory('outbound'))} 
              saving={saving} 
              onActivate={handleMakeActiveOutbound} 
              onConfigure={handleConfigure} 
              registry={registry} 
              icon={<Phone />}
            />
            
            <Section 
              title="Storage Providers" 
              items={byCategory('storage')} 
              saving={saving} 
              onActivate={undefined} 
              onConfigure={handleConfigure} 
              registry={registry} 
              icon={<StorageIcon />}
            />
            
            <Discover 
              title="Discover (Curated Registry)" 
              items={registryOnly()} 
              icon={<CloudDownloadIcon />}
            />
            
            {/* HTTP Manifest Tester */}
            <ResponsiveFormSection
              title="HTTP Manifest Tester (Preview)"
              subtitle="Paste a manifest JSON and validate or dry‑run a send. Installing saves to the providers directory."
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
                    
                    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                      <ResponsiveTextField
                        label="To Number" 
                        value={manifestTo} 
                        onChange={setManifestTo}
                        icon={<Phone />}
                      />
                      <ResponsiveTextField
                        label="File URL (tokenized)" 
                        value={manifestFileUrl} 
                        onChange={setManifestFileUrl}
                        placeholder="https://.../file.pdf?token=..."
                      />
                    </Stack>
                    
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
                        variant="outlined" 
                        onClick={async () => {
                          try {
                            setError(''); setNote(''); setManifestResult(null);
                            const parsed = JSON.parse(manifestJson || '{}');
                            const r = await client.validateHttpManifest({ 
                              manifest: parsed, 
                              to: manifestTo, 
                              file_url: manifestFileUrl, 
                              render_only: false 
                            });
                            setManifestResult(r);
                          } catch (e: any) {
                            setError(e?.message || 'Dry‑run failed');
                          }
                        }}
                        sx={{ borderRadius: 2 }}
                      >
                        Dry‑run Send
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
                    </Stack>
                    
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
                            setNote(`Imported ${res.imported?.length || 0} provider(s)`);
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
                            const res = await client.importHttpManifests({ source: 'repo_scrape' } as any);
                            setBulkImportRes(res);
                            setNote(`Imported ${res.imported?.length || 0} provider(s) from repo scrape`);
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
              onClose={() => setConfigOpen(false)}
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
  onActivate, 
  onConfigure, 
  registry,
  icon 
}: { 
  title: string; 
  items: PluginItem[]; 
  saving: string | null; 
  onActivate?: (id: string) => void; 
  onConfigure?: (p: PluginItem) => void; 
  registry: PluginItem[];
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
          const reg = regIndex.get(p.id);
          const desc = p.description || reg?.description;
          const learn = (reg as any)?.learn_more as string | undefined;
          
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
                      label={p.enabled ? 'Enabled' : 'Disabled'} 
                      color={p.enabled ? 'success' : 'default'}
                      sx={{ borderRadius: 1 }}
                    />
                  </Box>
                  
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
                      <Tooltip title="Edit non‑secret settings for this plugin">
                        <Button 
                          size="small" 
                          onClick={() => onConfigure(p)}
                          sx={{ borderRadius: 1 }}
                        >
                          Configure
                        </Button>
                      </Tooltip>
                    )}
                    {onActivate ? (
                      <Tooltip title="Mark this provider active in the config file (no live apply)">
                        <span style={{ marginLeft: 'auto' }}>
                          <Button 
                            size="small" 
                            variant="contained" 
                            disabled={saving === p.id} 
                            onClick={() => onActivate(p.id)}
                            startIcon={saving === p.id ? <CircularProgress size={16} /> : undefined}
                            sx={{ borderRadius: 1 }}
                          >
                            {saving === p.id ? 'Saving…' : 'Set Active'}
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

function Discover({ title, items, icon }: { title: string; items: any[]; icon?: React.ReactNode }) {
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
                    <MLink href={r.learn_more} target="_blank" rel="noreferrer" sx={{ fontSize: '0.875rem' }}>
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