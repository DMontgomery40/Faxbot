// System → Developer: the configuration summary, security status and SDK
// quickstart that used to sit on the Dashboard. Developer vocabulary belongs here.
import { useCallback, useEffect, useState } from 'react';
import { Box, Button, Card, CardContent, Chip, Grid, IconButton, Tooltip, Typography } from '@mui/material';
import {
  CheckCircle as CheckCircleIcon,
  ContentCopy as ContentCopyIcon,
  Error as ErrorIcon,
  Refresh as RefreshIcon,
} from '@mui/icons-material';
import type AdminAPIClient from '../api/client';
import type { HealthStatus } from '../api/types';
import { providerLabel } from '../providerLabels';

function useConfig(client: AdminAPIClient) {
  const [cfg, setCfg] = useState<any | null>(null);
  const load = useCallback(async () => {
    try {
      setCfg(await client.getConfig());
    } catch {
      setCfg(null);
    }
  }, [client]);
  useEffect(() => { void load(); }, [load]);
  return { cfg, load };
}

// The MCP endpoints and OAuth requirement, shown above the AI assistants settings.
export function AssistantsOverview({ client }: { client: AdminAPIClient }) {
  const { cfg } = useConfig(client);
  return (
    <Card sx={{ my: 3 }}>
      <CardContent>
        <Typography variant="h6" gutterBottom>MCP Overview</Typography>
        <Grid container spacing={1} alignItems="center">
          <Grid item xs={4}><Typography variant="body2" color="text.secondary">SSE</Typography></Grid>
          <Grid item xs={8}>
            <Chip size="small" label={cfg?.mcp?.sse_enabled ? 'Enabled' : 'Disabled'} color={cfg?.mcp?.sse_enabled ? 'success' : 'default'} variant="outlined" />
            {cfg?.mcp?.sse_enabled && (
              <Tooltip title="Copy SSE URL">
                <IconButton size="small" sx={{ ml: 1 }} onClick={() => navigator.clipboard.writeText(`${window.location.origin}${cfg?.mcp?.sse_path || '/mcp/sse'}`)}>
                  <ContentCopyIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            )}
          </Grid>
          <Grid item xs={4}><Typography variant="body2" color="text.secondary">HTTP</Typography></Grid>
          <Grid item xs={8}>
            <Chip size="small" label={cfg?.mcp?.http_enabled ? 'Enabled' : 'Disabled'} color={cfg?.mcp?.http_enabled ? 'success' : 'default'} variant="outlined" />
            {cfg?.mcp?.http_enabled && (
              <Tooltip title="Copy HTTP URL">
                <IconButton size="small" sx={{ ml: 1 }} onClick={() => navigator.clipboard.writeText(`${window.location.origin}${cfg?.mcp?.http_path || '/mcp/http'}`)}>
                  <ContentCopyIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            )}
          </Grid>
          <Grid item xs={4}><Typography variant="body2" color="text.secondary">OAuth</Typography></Grid>
          <Grid item xs={8}><Chip size="small" label={cfg?.mcp?.require_oauth ? 'Required' : 'Optional'} variant="outlined" /></Grid>
        </Grid>
      </CardContent>
    </Card>
  );
}

export default function DeveloperOverview({ client }: { client: AdminAPIClient }) {
  const { cfg, load } = useConfig(client);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [plugins, setPlugins] = useState<any[] | null>(null);

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await client.getHealthStatus());
    } catch {
      setHealth(null);
    }
  }, [client]);

  useEffect(() => { void loadHealth(); }, [loadHealth]);

  // The plugins list loads only when v3 plugins are enabled.
  useEffect(() => {
    if (!cfg?.v3_plugins?.enabled) {
      setPlugins(null);
      return;
    }
    let current = true;
    client.listPlugins().then((list) => { if (current) setPlugins(list?.items || []); })
      .catch(() => { if (current) setPlugins([]); });
    return () => { current = false; };
  }, [client, cfg?.v3_plugins?.enabled]);

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">API & SDKs</Typography>
        <Button variant="outlined" startIcon={<RefreshIcon />} onClick={() => { void load(); void loadHealth(); }}>
          Refresh
        </Button>
      </Box>
      <Grid container spacing={{ xs: 2, md: 3 }}>
        {/* Config Overview */}
        <Grid item xs={12} md={6}>
          <Card sx={{ height: '100%' }}>
            <CardContent>
              <Typography variant="h6" gutterBottom>Config Overview</Typography>
              <Grid container spacing={1}>
                <Grid item xs={6}><Typography variant="body2" color="text.secondary">Sending</Typography></Grid>
                <Grid item xs={6}><Chip size="small"
                  label={cfg ? (cfg.hybrid?.outbound ? providerLabel(cfg.hybrid.outbound) : 'No provider') : 'Unavailable'} /></Grid>
                <Grid item xs={6}><Typography variant="body2" color="text.secondary">Receiving</Typography></Grid>
                <Grid item xs={6}><Chip size="small"
                  label={cfg ? (cfg.inbound?.enabled && cfg.hybrid?.inbound ? providerLabel(cfg.hybrid.inbound) : 'No provider') : 'Unavailable'} /></Grid>
                <Grid item xs={6}><Typography variant="body2" color="text.secondary">Storage</Typography></Grid>
                <Grid item xs={6}><Chip size="small" label={cfg?.storage?.backend || 'local'} /></Grid>
                <Grid item xs={6}><Typography variant="body2" color="text.secondary">Authentication</Typography></Grid>
                <Grid item xs={6}><Chip size="small" label="Required" color="success" variant="outlined" /></Grid>
                <Grid item xs={6}><Typography variant="body2" color="text.secondary">Enforce HTTPS</Typography></Grid>
                <Grid item xs={6}><Chip size="small" label={(cfg?.enforce_public_https ? 'Enabled' : 'Disabled')} color={cfg?.enforce_public_https ? 'success' : 'default'} variant="outlined" /></Grid>
                <Grid item xs={6}><Typography variant="body2" color="text.secondary">v3 Plugins</Typography></Grid>
                <Grid item xs={6}><Chip size="small" label={(cfg?.v3_plugins?.enabled ? `Enabled (${cfg?.v3_plugins?.active_outbound ? providerLabel(cfg.v3_plugins.active_outbound) : '-'})` : 'Disabled')} color={cfg?.v3_plugins?.enabled ? 'success' : 'default'} variant="outlined" /></Grid>
                <Grid item xs={6}><Typography variant="body2" color="text.secondary">Held Test Faxes</Typography></Grid>
                <Grid item xs={6}><Chip size="small" data-testid="held-test-faxes" label={String(health?.jobs.held ?? 'Unavailable')} variant="outlined" /></Grid>
              </Grid>
            </CardContent>
          </Card>
        </Grid>

        {/* Security Status */}
        <Grid item xs={12} md={6}>
          <Card sx={{ height: '100%' }}>
            <CardContent>
              <Typography variant="h6" component="h2" gutterBottom>Security</Typography>
              <Box display="flex" flexDirection="column" gap={1}>
                <Box display="flex" alignItems="center">
                  <CheckCircleIcon color="success" />
                  <Typography variant="body2" sx={{ ml: 1 }}>Authentication required</Typography>
                </Box>
                {health && (
                  <Box display="flex" alignItems="center">
                    {health.api_keys_configured ? <CheckCircleIcon color="success" /> : <ErrorIcon color="error" />}
                    <Typography variant="body2" sx={{ ml: 1 }}>
                      {health.api_keys_configured ? 'API Keys Configured' : 'No API Keys'}
                    </Typography>
                  </Box>
                )}
              </Box>
            </CardContent>
          </Card>
        </Grid>

        {/* Plugins (feature-gated) */}
        {cfg?.v3_plugins?.enabled && (
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>Plugins</Typography>
                <Typography variant="body2" color="text.secondary">
                  Outbound: {cfg?.v3_plugins?.active_outbound || '-'} • Installed: {plugins?.length ?? 0}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  Manifest warnings appear under Diagnostics.
                </Typography>
              </CardContent>
            </Card>
          </Grid>
        )}

        {/* SDK & Quickstart */}
        <Grid item xs={12} md={cfg?.v3_plugins?.enabled ? 6 : 12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>SDK & Quickstart</Typography>
              <Typography variant="body2">Base URL: {window.location.origin}</Typography>
              <Typography variant="body2" sx={{ mb: 1 }}>Header: X-API-Key: &lt;your key&gt;</Typography>
              <Typography variant="body2" color="text.secondary">Node:</Typography>
              <Box component="pre" sx={{ p: 1, bgcolor: 'background.default', borderRadius: 1, overflow: 'auto' }}>{`npm i faxbot@1.0.2
node -e "(async()=>{const FaxbotClient=require('faxbot');const c=new FaxbotClient('${window.location.origin}','<key>');const r=await c.sendFax('+15551234567','/path/to/file.pdf');console.log(r)})()"`}</Box>
              <Typography variant="body2" color="text.secondary">Python:</Typography>
              <Box component="pre" sx={{ p: 1, bgcolor: 'background.default', borderRadius: 1, overflow: 'auto' }}>{`pip install faxbot==1.0.2
python - <<'PY'
from faxbot import FaxbotClient
c=FaxbotClient('${window.location.origin}','<key>')
print(c.send_fax('+15551234567','/path/to/file.pdf'))
PY`}</Box>
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}
