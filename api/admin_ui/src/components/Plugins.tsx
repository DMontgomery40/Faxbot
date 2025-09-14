import { useEffect, useState } from 'react';
import { Box, Grid, Card, CardContent, CardActions, Typography, Button, Chip, Link as MLink, Tooltip, Divider, Alert } from '@mui/material';
import { Extension, Cloud, Storage as StorageIcon, Phone, WarningAmber } from '@mui/icons-material';
import AdminAPIClient from '../api/client';

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

  const byCategory = (cat: string) => (items || []).filter(p => (p.categories || []).includes(cat));

  return (
    <Box sx={{ mt: 2 }}>
      <Typography variant="h5" gutterBottom>Plugins</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Manage provider plugins. This preview lists installed providers; updates persist to the config file only. No live apply yet.
      </Typography>
      <Alert severity="info" sx={{ mb: 2 }}>
        Plugin changes are feature‑gated and safe to explore. Outbound provider remains controlled by env (<code>FAX_BACKEND</code>) until an explicit apply flow is added.
      </Alert>
      {note && <Alert severity="success" onClose={() => setNote('')} sx={{ mb: 2 }}>{note}</Alert>}
      {error && <Alert severity="error" onClose={() => setError('')} sx={{ mb: 2 }}>{error}</Alert>}
      {loading ? (
        <Typography>Loading…</Typography>
      ) : (
        <>
          <Section title="Outbound Providers" items={byCategory('outbound')} saving={saving} onActivate={handleMakeActiveOutbound} registry={registry} />
          <Divider sx={{ my: 3 }} />
          <Section title="Storage Providers" items={byCategory('storage')} saving={saving} onActivate={undefined} registry={registry} />
        </>
      )}
    </Box>
  );
}

function Section({ title, items, saving, onActivate, registry }: { title: string; items: PluginItem[]; saving: string | null; onActivate?: (id: string) => void; registry: PluginItem[]; }) {
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
