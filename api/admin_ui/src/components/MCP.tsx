import { useEffect, useRef, useState } from 'react';
import { Box, Card, CardContent, Typography, Button, Alert, Grid, TextField, Switch, FormControlLabel, Chip, Tooltip, IconButton, Paper } from '@mui/material';
import { ContentCopy } from '@mui/icons-material';
import AdminAPIClient, { configurationWriteRejected } from '../api/client';
import type { ConfigurationWriteResult, Settings, SettingsPatch } from '../api/types';

interface MCPProps { client: AdminAPIClient; }
type MCPConfiguration = NonNullable<Settings['mcp']>;

const editorValues = (mcp: MCPConfiguration): SettingsPatch => ({
  enable_mcp_sse: mcp.sse_enabled,
  enable_mcp_http: mcp.http_enabled,
  require_mcp_oauth: mcp.require_oauth,
  oauth_issuer: mcp.oauth.issuer,
  oauth_audience: mcp.oauth.audience,
  oauth_jwks_url: mcp.oauth.jwks_url,
});

function MCP({ client }: MCPProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [snack, setSnack] = useState<string | null>(null);
  const [sseEnabled, setSseEnabled] = useState(false);
  const [httpEnabled, setHttpEnabled] = useState(false);
  const [requireOAuth, setRequireOAuth] = useState(false);
  const [issuer, setIssuer] = useState('');
  const [audience, setAudience] = useState('');
  const [jwks, setJwks] = useState('');
  const [health, setHealth] = useState<'SSE' | 'HTTP' | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [baseline, setBaseline] = useState<SettingsPatch>({});
  const [activeMcp, setActiveMcp] = useState<MCPConfiguration | null>(null);
  const [needsReload, setNeedsReload] = useState(true);
  const [saveResult, setSaveResult] = useState<ConfigurationWriteResult | null>(null);
  const actionFence = useRef(false);
  const requestEpoch = useRef(0);
  const desiredRevision = needsReload ? undefined : settings?._meta?.desired_revision_id;
  const canEdit = !!desiredRevision && !loading && !needsReload;
  const revisionMeta = needsReload && saveResult ? saveResult._meta : settings?._meta;
  const pendingCount = needsReload ? 0 : settings?._meta?.pending_fields?.length ?? 0;

  const readSnapshot = async (epoch: number) => {
    const [desired, active] = await Promise.all([client.getSettings(), client.getMcpConfig()]);
    if (epoch !== requestEpoch.current) return;
    if (!desired._meta?.desired_revision_id || !desired.mcp || !active?.mcp) {
      throw new Error('MCP settings could not be loaded. Refresh to try again.');
    }
    const mcp = desired.mcp;
    setSettings(desired);
    setBaseline(editorValues(mcp));
    setSseEnabled(mcp.sse_enabled);
    setHttpEnabled(mcp.http_enabled);
    setRequireOAuth(mcp.require_oauth);
    setIssuer(mcp.oauth.issuer);
    setAudience(mcp.oauth.audience);
    setJwks(mcp.oauth.jwks_url);
    setActiveMcp(active.mcp);
    setNeedsReload(false);
    setHealth(null);
    const transport = active.mcp.http_enabled ? 'HTTP' : active.mcp.sse_enabled ? 'SSE' : null;
    if (transport) {
      const path = transport === 'SSE' ? active.mcp.sse_path : active.mcp.http_path;
      try {
        await client.getMcpHealth(`${path}/health`);
        if (epoch === requestEpoch.current) setHealth(transport);
      } catch { /* Health stays unconfirmed; the settings above remain editable. */ }
    }
  };

  const load = async () => {
    if (actionFence.current) return;
    const epoch = ++requestEpoch.current;
    setLoading(true);
    setNeedsReload(true);
    setError(null);
    try {
      await readSnapshot(epoch);
    } catch (e: any) {
      if (epoch === requestEpoch.current) setError(e?.message || 'Failed to load MCP config');
    } finally {
      if (epoch === requestEpoch.current) setLoading(false);
    }
  };

  useEffect(() => {
    actionFence.current = false;
    setSettings(null);
    setBaseline({});
    setActiveMcp(null);
    setHealth(null);
    setSaveResult(null);
    setSnack(null);
    setSseEnabled(false);
    setHttpEnabled(false);
    setRequireOAuth(false);
    setIssuer('');
    setAudience('');
    setJwks('');
    void load();
    return () => { requestEpoch.current += 1; };
  }, [client]);

  const apply = async () => {
    if (!canEdit || actionFence.current || !desiredRevision) return;
    const values: SettingsPatch = {
      enable_mcp_sse: sseEnabled,
      enable_mcp_http: httpEnabled,
      require_mcp_oauth: requireOAuth,
      oauth_issuer: issuer,
      oauth_audience: audience,
      oauth_jwks_url: jwks,
    };
    const changed = Object.entries(values).filter(([key, value]) => value !== baseline[key]);
    if (!changed.length) {
      setSnack('Nothing changed.');
      return;
    }
    actionFence.current = true;
    const epoch = requestEpoch.current;
    setLoading(true); setError(null); setSnack(null);
    setSaveResult(null);
    try {
      const writeResult = await client.updateSettings({ expected_revision_id: desiredRevision, ...Object.fromEntries(changed) });
      if (epoch !== requestEpoch.current) return;
      setSaveResult(writeResult);
      setNeedsReload(true);
      setHealth(null);
      setActiveMcp(null);
      setSnack(writeResult._meta.apply_state === 'pending_restart'
        ? `${writeResult.changed ? 'Settings saved.' : 'Nothing changed.'} Restart Faxbot to apply pending changes.`
        : writeResult.changed ? 'Settings saved.' : 'Nothing changed.');
      try {
        await readSnapshot(epoch);
      } catch {
        if (epoch === requestEpoch.current) setError('MCP settings could not be reloaded. Refresh to keep editing.');
      }
    } catch (e: any) {
      if (epoch !== requestEpoch.current) return;
      setNeedsReload(true);
      setError((e?.message || '').includes('409')
        ? 'Someone else changed these settings. Your edits are kept here; refresh to see the current values.'
        : configurationWriteRejected(e)
          ? `Settings were not saved (error ${e.status}). Refresh to try again.`
          : 'Faxbot could not confirm the save. Refresh to check the current values.');
    } finally {
      if (epoch === requestEpoch.current) {
        actionFence.current = false;
        setLoading(false);
      }
    }
  };

  const sseUrl = () => `${window.location.origin}${activeMcp?.sse_path || '/mcp/sse'}`;
  const httpUrl = () => `${window.location.origin}${activeMcp?.http_path || '/mcp/http'}`;

  // Remote MCP clients prefer Streamable HTTP; SSE remains for older clients.
  // Each client presents its own Faxbot API key, which the server forwards.
  const preferHttp = Boolean(activeMcp?.http_enabled || !activeMcp?.sse_enabled);
  const generateClientConfig = () => JSON.stringify({
    mcpServers: {
      faxbot: {
        type: preferHttp ? 'http' : 'sse',
        url: preferHttp ? httpUrl() : sseUrl(),
        headers: activeMcp?.require_oauth
          ? { Authorization: 'Bearer <YOUR_JWT>' }
          : { 'X-API-Key': '<this client\'s Faxbot API key>' },
      },
    },
  }, null, 2);

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">MCP Integration</Typography>
        {health ? (
          <Chip label="MCP server responding" color="success" variant="outlined" />
        ) : (
          <Chip label={activeMcp && !activeMcp.sse_enabled && !activeMcp.http_enabled ? 'MCP disabled' : 'Health not confirmed'} color="warning" variant="outlined" />
        )}
      </Box>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {snack && <Alert severity={saveResult?._meta.apply_state === 'pending_restart' ? 'warning' : 'success'} sx={{ mb: 2 }} onClose={() => setSnack(null)}>{snack}</Alert>}
      {revisionMeta?.apply_state === 'pending_restart' && !(snack && saveResult?._meta.apply_state === 'pending_restart') && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {pendingCount > 0
            ? `Restart Faxbot to apply ${pendingCount} pending ${pendingCount === 1 ? 'change' : 'changes'}.`
            : 'Restart Faxbot to apply pending changes.'}
        </Alert>
      )}
      {needsReload && !loading && !error && <Alert severity="warning" sx={{ mb: 2 }}>Refresh to load the current settings.</Alert>}

      <Grid container spacing={3}>
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Server Settings</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                Turn on the transports your AI assistant uses to connect to Faxbot.
              </Typography>
              <Box component="fieldset" disabled={!canEdit} sx={{ border: 0, m: 0, p: 0, minWidth: 0 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                <FormControlLabel control={<Switch checked={sseEnabled} onChange={(e) => setSseEnabled(e.target.checked)} />} label="SSE (older clients)" />
                {settings?.mcp?.sse_path && <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>{settings.mcp.sse_path}</Typography>}
              </Box>
              <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                <FormControlLabel control={<Switch checked={httpEnabled} onChange={(e) => setHttpEnabled(e.target.checked)} />} label="Streamable HTTP (recommended)" />
                {settings?.mcp?.http_path && <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>{settings.mcp.http_path}</Typography>}
              </Box>
              <FormControlLabel control={<Switch checked={requireOAuth} onChange={(e) => setRequireOAuth(e.target.checked)} />} label="Require OAuth (JWT)" />
              {requireOAuth && (
                <Box sx={{ display: 'grid', gridTemplateColumns: '1fr', gap: 2, mt: 2 }}>
                  <TextField label="Issuer (OAUTH_ISSUER)" value={issuer} onChange={(e) => setIssuer(e.target.value)} fullWidth size="small" />
                  <TextField label="Audience (OAUTH_AUDIENCE)" value={audience} onChange={(e) => setAudience(e.target.value)} fullWidth size="small" />
                  <TextField label="JWKS URL (OAUTH_JWKS_URL)" value={jwks} onChange={(e) => setJwks(e.target.value)} fullWidth size="small" />
                </Box>
              )}
              </Box>
              <Box sx={{ mt: 2, display: 'flex', gap: 1 }}>
                <Button variant="contained" onClick={apply} disabled={!canEdit}>Apply & Reload</Button>
                <Button variant="outlined" onClick={load} disabled={loading}>Refresh</Button>
              </Box>
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Client configuration</Typography>
              {!activeMcp ? <Alert severity="info">The client configuration appears once MCP settings load.</Alert> : <>
              {!activeMcp.sse_enabled && !activeMcp.http_enabled && <Alert severity="warning" sx={{ mb: 2 }}>Turn on a transport above so clients can connect.</Alert>}
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Give each AI client its own API key from Keys, then add this to its MCP configuration.
              </Typography>
              <Paper sx={{ position: 'relative', p: 2, bgcolor: 'background.default', borderRadius: 1, overflow: 'auto' }}>
                <Box component="pre" sx={{ m: 0 }}>{generateClientConfig()}</Box>
                <Tooltip title="Copy config">
                  <IconButton size="small" sx={{ position: 'absolute', right: 8, top: 8 }} onClick={() => navigator.clipboard.writeText(generateClientConfig())}>
                    <ContentCopy fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Paper>
              {activeMcp.http_enabled && activeMcp.sse_enabled && (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 2 }}>
                  <Typography variant="body2" color="text.secondary">Older clients that need SSE: {sseUrl()}</Typography>
                  <Tooltip title="Copy SSE URL"><IconButton size="small" onClick={() => navigator.clipboard.writeText(sseUrl())}><ContentCopy fontSize="small" /></IconButton></Tooltip>
                </Box>
              )}
              </>}
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Stdio (Advanced)</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Prefer SSE/HTTP for zero‑terminal setup. Stdio is an advanced option for local assistants that speak MCP over stdio.
              </Typography>
              <Box component="pre" sx={{ p: 2, bgcolor: 'background.default', borderRadius: 1, overflow: 'auto' }}>
{`# Optional stdio server (advanced)
cd python_mcp
python stdio_server.py  # Requires FAX_API_URL and API_KEY in environment

# Claude Desktop (example)
{
  "mcpServers": {
    "faxbot": {
      "command": "python",
      "args": ["/app/python_mcp/stdio_server.py"],
      "env": {"FAX_API_URL": "${window.location.origin}", "API_KEY": "<your_api_key>"}
    }
  }
}`}
              </Box>
              <Alert severity="warning" sx={{ mt: 2 }}>
                For HIPAA, avoid stdio unless the assistant process and transport are fully controlled within your private environment.
              </Alert>
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}

export default MCP;
