import { useEffect, useRef, useState } from 'react';
import { Box, Card, CardContent, Typography, Button, Alert, Grid, TextField, Switch, FormControlLabel, Chip, Tooltip, IconButton, Paper } from '@mui/material';
import { ContentCopy } from '@mui/icons-material';
import AdminAPIClient, { configurationWriteRejected } from '../api/client';
import type { ConfigurationWriteReceipt, Settings, SettingsPatch } from '../api/types';

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
  const [saveReceipt, setSaveReceipt] = useState<ConfigurationWriteReceipt | null>(null);
  const actionFence = useRef(false);
  const requestEpoch = useRef(0);
  const desiredRevision = needsReload ? undefined : settings?._meta?.desired_revision_id;
  const canEdit = !!desiredRevision && !loading && !needsReload;
  const revisionMeta = needsReload && saveReceipt ? saveReceipt._meta : settings?._meta;

  const readSnapshot = async (epoch: number) => {
    const [desired, active] = await Promise.all([client.getSettings(), client.getMcpConfig()]);
    if (epoch !== requestEpoch.current) return;
    if (!desired._meta?.desired_revision_id || !desired.mcp || !active?.mcp) {
      throw new Error('A canonical desired revision and active MCP settings are required before editing.');
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
    const transport = active.mcp.sse_enabled ? 'SSE' : active.mcp.http_enabled ? 'HTTP' : null;
    if (transport) {
      const path = transport === 'SSE' ? active.mcp.sse_path : active.mcp.http_path;
      try {
        await client.getMcpHealth(`${path}/health`);
        if (epoch === requestEpoch.current) setHealth(transport);
      } catch { /* Health remains unconfirmed; desired settings are still readable. */ }
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
    setSaveReceipt(null);
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
      setSnack('No changes to save. The loaded desired MCP settings are unchanged.');
      return;
    }
    actionFence.current = true;
    const epoch = requestEpoch.current;
    setLoading(true); setError(null); setSnack(null);
    setSaveReceipt(null);
    try {
      const receipt = await client.updateSettings({ expected_revision_id: desiredRevision, ...Object.fromEntries(changed) });
      if (epoch !== requestEpoch.current) return;
      setSaveReceipt(receipt);
      setNeedsReload(true);
      setHealth(null);
      setActiveMcp(null);
      setSnack(receipt._meta.apply_state === 'pending_restart'
        ? `${receipt.changed ? 'Desired MCP settings saved durably.' : 'Desired MCP settings are unchanged.'} Every API worker must stop and the installation restart to activate the pending revision.`
        : receipt.changed ? 'MCP settings saved durably and active.' : 'MCP settings are unchanged and active.');
      try {
        await readSnapshot(epoch);
      } catch {
        if (epoch === requestEpoch.current) setError('Save confirmed; the MCP settings view could not be reloaded. Editing is paused. Refresh explicitly, or sign in again, before another save.');
      }
    } catch (e: any) {
      if (epoch !== requestEpoch.current) return;
      setNeedsReload(true);
      setError((e?.message || '').includes('409')
        ? 'Settings changed after this editor loaded. Your draft is retained. Refresh explicitly to discard it and review the current desired revision before saving again.'
        : `${configurationWriteRejected(e) ? 'Save was rejected. Your draft is retained.' : 'Save was not confirmed.'} ${e?.message || ''} Refresh to check the current configuration before saving again.`);
    } finally {
      if (epoch === requestEpoch.current) {
        actionFence.current = false;
        setLoading(false);
      }
    }
  };

  const sseUrl = () => `${window.location.origin}${activeMcp?.sse_path || '/mcp/sse'}`;
  const httpUrl = () => `${window.location.origin}${activeMcp?.http_path || '/mcp/http'}`;

  const generateClaudeConfig = () => {
    const cfg: any = {
      mcpServers: {
        faxbot: {
          transport: 'sse',
          url: sseUrl(),
        }
      }
    };
    if (!activeMcp?.require_oauth) {
      cfg.mcpServers.faxbot.headers = { };
    } else {
      cfg.mcpServers.faxbot.headers = { 'authorization': 'Bearer <YOUR_JWT>' };
    }
    return JSON.stringify(cfg, null, 2);
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">MCP Integration</Typography>
        {health ? (
          <Chip label={`Active ${health} healthy`} color="success" variant="outlined" />
        ) : (
          <Chip label={activeMcp && !activeMcp.sse_enabled && !activeMcp.http_enabled ? 'Active MCP disabled' : 'Active MCP health unconfirmed'} color="warning" variant="outlined" />
        )}
      </Box>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {snack && <Alert severity={saveReceipt?._meta.apply_state === 'pending_restart' ? 'warning' : 'success'} sx={{ mb: 2 }} onClose={() => setSnack(null)}>{snack}</Alert>}
      {revisionMeta && <Alert severity={revisionMeta.apply_state === 'pending_restart' ? 'warning' : 'info'} sx={{ mb: 2 }}>
        {needsReload && saveReceipt ? 'Confirmed saved' : 'Loaded'} desired revision: {revisionMeta.desired_revision_id}. Active revision: {revisionMeta.active_revision_id}. Generation: {revisionMeta.generation}.
        {revisionMeta.apply_state === 'pending_restart' && ' Active behavior continues until every worker stops and the installation restarts.'}
      </Alert>}
      {needsReload && <Alert severity="warning" sx={{ mb: 2 }}>Editing and saving are paused. Refresh explicitly to load the current desired revision; this discards the visible draft.</Alert>}

      <Grid container spacing={3}>
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Server Settings</Typography>
              <Alert severity="info" sx={{ mb: 2 }}>
                Embedded Python MCP servers use the transport paths configured in installation settings when enabled. No external Node process is required for SSE/HTTP.
              </Alert>
              <Box component="fieldset" disabled={!canEdit} sx={{ border: 0, m: 0, p: 0, minWidth: 0 }}>
              <Typography variant="body2" sx={{ mb: 2 }}>These controls edit desired MCP settings. Client examples and health below use separately loaded active settings.</Typography>
              <FormControlLabel control={<Switch checked={sseEnabled} onChange={(e) => setSseEnabled(e.target.checked)} />} label="Enable SSE" />
              <FormControlLabel control={<Switch checked={httpEnabled} onChange={(e) => setHttpEnabled(e.target.checked)} />} label="Enable Streamable HTTP" />
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
              <Typography variant="h6" gutterBottom>Claude Desktop Config</Typography>
              {!activeMcp ? <Alert severity="info">Reload active MCP settings before copying a client configuration.</Alert> : <>
              {!activeMcp.sse_enabled && <Alert severity="warning" sx={{ mb: 2 }}>SSE is disabled in the loaded active configuration. This client example will be usable only after SSE is active.</Alert>}
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Add to your Claude Desktop MCP config. SSE URL: {sseUrl()}
              </Typography>
              <Paper sx={{ position: 'relative', p: 2, bgcolor: 'background.default', borderRadius: 1, overflow: 'auto' }}>
                <Box component="pre" sx={{ m: 0 }}>{generateClaudeConfig()}</Box>
                <Tooltip title="Copy config">
                  <IconButton size="small" sx={{ position: 'absolute', right: 8, top: 8 }} onClick={() => navigator.clipboard.writeText(generateClaudeConfig())}>
                    <ContentCopy fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Paper>
              {activeMcp.http_enabled && (
                <>
                  <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>HTTP Transport (experimental)</Typography>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography variant="body2" color="text.secondary">HTTP URL: {httpUrl()}</Typography>
                    <Tooltip title="Copy HTTP URL"><IconButton size="small" onClick={() => navigator.clipboard.writeText(httpUrl())}><ContentCopy fontSize="small" /></IconButton></Tooltip>
                  </Box>
                </>
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
