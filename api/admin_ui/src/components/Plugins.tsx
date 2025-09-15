import { useEffect, useState } from 'react';
import { Box, Grid, Card, CardContent, CardActions, Typography, Button, Chip, Link as MLink, Tooltip, Divider, Alert, TextField } from '@mui/material';
import { Extension, Cloud, Storage as StorageIcon, Phone, WarningAmber } from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import PluginConfigDialog from './PluginConfigDialog';

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

// Shared icon helper (module scope so Section can use it)
const iconFor = (cat: string) => {
  switch ((cat || '').toLowerCase()) {
    case 'outbound': return <Phone fontSize="small" sx={{ mr: 0.5 }} />;
    case 'storage': return <StorageIcon fontSize="small" sx={{ mr: 0.5 }} />;
    default: return <Extension fontSize="small" sx={{ mr: 0.5 }} />;
  }
};

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
  // Manifest tester state
  const [manifestJson, setManifestJson] = useState<string>('');
  const [manifestTo, setManifestTo] = useState<string>('+15551234567');
  const [manifestFileUrl, setManifestFileUrl] = useState<string>('');
  const [manifestResult, setManifestResult] = useState<any | null>(null);

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
  // Ensure Documo appears even if server list is behind
  const ensureDocumo = (arr: PluginItem[]) => {
    const has = arr.some(p => p.id === 'documo');
    if (!has) {
      arr = arr.concat([{ id: 'documo', name: 'Documo mFax', version: '1.0.0', categories: ['outbound'], capabilities: ['send','get_status'], description: 'Direct upload (preview)' } as any]);
    }
    return arr;
  };
  const registryOnly = () => {
    const installed = new Set((items || []).map(i => i.id));
    return (registry || []).filter(r => !installed.has(r.id) && matches(r as any));
  };

  return (
    <Box sx={{ mt: 2 }}>
      <Typography variant="h5" gutterBottom>Plugins</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Manage provider plugins. This preview lists installed providers; updates persist to the config file only. No live apply yet.
      </Typography>
      <Box sx={{ display: 'flex', gap: 1, mb: 2 }}>
        <TextField size="small" fullWidth placeholder="Search curated plugins…" value={query} onChange={(e)=>setQuery(e.target.value)} />
      </Box>
      <Alert severity="info" sx={{ mb: 2 }}>
        Plugin changes are feature‑gated and safe to explore. Outbound provider remains controlled by env (<code>FAX_BACKEND</code>) until an explicit apply flow is added.
      </Alert>
      {note && <Alert severity="success" onClose={() => setNote('')} sx={{ mb: 2 }}>{note}</Alert>}
      {error && <Alert severity="error" onClose={() => setError('')} sx={{ mb: 2 }}>{error}</Alert>}
      {loading ? (
        <Typography>Loading…</Typography>
      ) : (
        <>
          <Section title="Outbound Providers" items={ensureDocumo(byCategory('outbound'))} saving={saving} onActivate={handleMakeActiveOutbound} onConfigure={handleConfigure} registry={registry} />
          <Divider sx={{ my: 3 }} />
          <Section title="Storage Providers" items={byCategory('storage')} saving={saving} onActivate={undefined} onConfigure={handleConfigure} registry={registry} />
          <Divider sx={{ my: 3 }} />
          <Discover title="Discover (Curated Registry)" items={registryOnly()} />
          <Divider sx={{ my: 3 }} />
          <Box>
            <Typography variant="h6" sx={{ mb: 1 }}>HTTP Manifest Tester (Preview)</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Paste a manifest JSON and validate or dry‑run a send. Installing saves to the providers directory.
            </Typography>
            <Grid container spacing={2}>
              <Grid item xs={12} md={8}>
                <TextField label="Manifest JSON" value={manifestJson} onChange={(e)=>setManifestJson(e.target.value)} fullWidth multiline minRows={8} placeholder="{\n  \"id\": \"example\",\n  \"allowed_domains\": [\"api.example.com\"],\n  \"actions\": {\n    \"send_fax\": { \"method\": \"POST\", \"url\": \"https://api.example.com/fax\", \"body\": { \"kind\": \"json\", \"template\": \"{\\\"to\\\":\\\"{{ to }}\\\", \\\"file_url\\\":\\\"{{ file_url }}\\\"}\" }, \"response\": { \"job_id\": \"data.id\", \"status\": \"data.status\" } }\n  }\n}" />
              </Grid>
              <Grid item xs={12} md={4}>
                <TextField label="To Number" value={manifestTo} onChange={(e)=>setManifestTo(e.target.value)} fullWidth size="small" sx={{ mb: 1 }} />
                <TextField label="File URL (tokenized)" value={manifestFileUrl} onChange={(e)=>setManifestFileUrl(e.target.value)} fullWidth size="small" sx={{ mb: 1 }} placeholder="https://.../file.pdf?token=..." />
                <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                  <Button size="small" variant="outlined" onClick={async () => {
                    try {
                      setError(''); setNote(''); setManifestResult(null);
                      const parsed = JSON.parse(manifestJson || '{}');
                      const r = await client.validateHttpManifest({ manifest: parsed, render_only: true });
                      setManifestResult(r);
                    } catch (e: any) {
                      setError(e?.message || 'Validate failed');
                    }
                  }}>Validate</Button>
                  <Button size="small" variant="outlined" onClick={async () => {
                    try {
                      setError(''); setNote(''); setManifestResult(null);
                      const parsed = JSON.parse(manifestJson || '{}');
                      const r = await client.validateHttpManifest({ manifest: parsed, to: manifestTo, file_url: manifestFileUrl, render_only: false });
                      setManifestResult(r);
                    } catch (e: any) {
                      setError(e?.message || 'Dry‑run failed');
                    }
                  }}>Dry‑run Send</Button>
                  <Button size="small" variant="contained" onClick={async () => {
                    try {
                      setError(''); setNote('');
                      const parsed = JSON.parse(manifestJson || '{}');
                      const r = await client.installHttpManifest({ manifest: parsed });
                      setNote(`Installed manifest ${r.id}`);
                      await load();
                    } catch (e: any) {
                      setError(e?.message || 'Install failed');
                    }
                  }}>Install</Button>
                </Box>
                {manifestResult && (
                  <Box sx={{ mt: 2 }}>
                    <Typography variant="subtitle2">Result</Typography>
                    <pre style={{ margin: 0, fontSize: '0.75rem' }}>{JSON.stringify(manifestResult, null, 2)}</pre>
                  </Box>
                )}
              </Grid>
            </Grid>
          </Box>
          <PluginConfigDialog
            open={configOpen}
            plugin={configPlugin}
            initialConfig={configData}
            onClose={() => setConfigOpen(false)}
            onSave={handleSaveConfig}
          />
        </>
      )}
    </Box>
  );
}

function Section({ title, items, saving, onActivate, onConfigure, registry }: { title: string; items: PluginItem[]; saving: string | null; onActivate?: (id: string) => void; onConfigure?: (p: PluginItem) => void; registry: PluginItem[]; }) {
  const joinCaps = (caps: string[]) => caps.join(', ');
  const regIndex = new Map((registry || []).map(r => [r.id, r] as const));
  return (
    <Box>
      <Typography variant="h6" sx={{ mb: 1 }}>{title}</Typography>
      <Grid container spacing={2}>
        {(items || []).map(p => {
          const reg = regIndex.get(p.id);
          const desc = p.description || reg?.description;
          const learn = (reg as any)?.learn_more as string | undefined;
          return (
            <Grid item xs={12} md={6} lg={4} key={p.id}>
              <Card variant="outlined">
                <CardContent>
                  <Box display="flex" alignItems="center" justifyContent="space-between">
                    <Box display="flex" alignItems="center">
                      <Cloud fontSize="small" sx={{ mr: 1 }} />
                      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>{p.name}</Typography>
                    </Box>
                    <Chip size="small" label={p.enabled ? 'Enabled' : 'Disabled'} color={p.enabled ? 'success' : 'default'} />
                  </Box>
                  <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{desc || 'No description available.'}</Typography>
                  <Box sx={{ mt: 1 }}>
                    {(p.categories || []).map(cat => (
                      <Chip key={cat} size="small" variant="outlined" sx={{ mr: 0.5 }} icon={iconFor(cat)} label={cat} />
                    ))}
                  </Box>
                  <Box sx={{ mt: 1 }}>
                    <Typography variant="caption" color="text.secondary">Capabilities: {joinCaps(p.capabilities || []) || '—'}</Typography>
                  </Box>
                  {learn && (
                    <Box sx={{ mt: 1 }}>
                      <MLink href={learn} target="_blank" rel="noreferrer">Learn more</MLink>
                    </Box>
                  )}
                </CardContent>
                <CardActions>
                  {onConfigure && (
                    <Tooltip title="Edit non‑secret settings for this plugin">
                      <span>
                        <Button size="small" onClick={() => onConfigure(p)}>Configure</Button>
                      </span>
                    </Tooltip>
                  )}
                  {onActivate ? (
                    <Tooltip title="Mark this provider active in the config file (no live apply)">
                      <span>
                        <Button size="small" variant="contained" disabled={saving === p.id} onClick={() => onActivate(p.id)}>
                          {saving === p.id ? 'Saving…' : 'Set Active'}
                        </Button>
                      </span>
                    </Tooltip>
                  ) : (
                    <Tooltip title="Storage provider selection is controlled by server settings">
                      <span>
                        <Button size="small" disabled>Managed by server</Button>
                      </span>
                    </Tooltip>
                  )}
                </CardActions>
              </Card>
            </Grid>
          );
        })}
        {(!items || items.length === 0) && (
          <Grid item xs={12}>
            <Alert icon={<WarningAmber />} severity="warning">No plugins discovered.</Alert>
          </Grid>
        )}
      </Grid>
    </Box>
  );
}

function Discover({ title, items }: { title: string; items: any[] }) {
  return (
    <Box>
      <Typography variant="h6" sx={{ mb: 1 }}>{title}</Typography>
      {(!items || items.length === 0) ? (
        <Alert severity="info">No matches found in the curated registry.</Alert>
      ) : (
        <Grid container spacing={2}>
          {items.map((r) => (
            <Grid item xs={12} md={6} lg={4} key={r.id}>
              <Card variant="outlined">
                <CardContent>
                  <Box display="flex" alignItems="center" justifyContent="space-between">
                    <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>{r.name}</Typography>
                    <Chip size="small" label={r.version || '1.x'} />
                  </Box>
                  <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{r.description || 'No description.'}</Typography>
                  <Box sx={{ mt: 1 }}>
                    {(r.categories || []).map((cat: string) => (
                      <Chip key={cat} size="small" variant="outlined" sx={{ mr: 0.5 }} label={cat} />
                    ))}
                  </Box>
                </CardContent>
                <CardActions>
                  {r.learn_more ? (
                    <MLink href={r.learn_more} target="_blank" rel="noreferrer">Learn more</MLink>
                  ) : (
                    <Tooltip title="Remote install is disabled by default for security."><span><Button size="small" disabled>Install Disabled</Button></span></Tooltip>
                  )}
                </CardActions>
              </Card>
            </Grid>
          ))}
        </Grid>
      )}
    </Box>
  );
}
