import { useEffect, useState } from 'react';
import { Box, Card, CardContent, Typography, Button, Alert, Grid, TextField, Switch, FormControlLabel, Chip, Tooltip, IconButton, Paper } from '@mui/material';
import { ContentCopy } from '@mui/icons-material';
import AdminAPIClient from '../api/client';

interface MCPProps { client: AdminAPIClient; }

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
  const [health, setHealth] = useState<any | null>(null);

  const load = async () => {
    setError(null);
    try {
      const cfg = await client.getMcpConfig();
      const mcp = cfg?.mcp || {};
      setSseEnabled(!!mcp.sse_enabled);
      setHttpEnabled(!!mcp.http_enabled);
      setRequireOAuth(!!mcp.require_oauth);
      setIssuer(mcp.oauth?.issuer || '');
      setAudience(mcp.oauth?.audience || '');
      setJwks(mcp.oauth?.jwks_url || '');
      if (mcp.sse_enabled) {
        try { setHealth(await client.getMcpHealth(mcp.sse_path ? `${mcp.sse_path}/health` : '/mcp/sse/health')); } catch {}
      } else if (mcp.http_enabled) {
        try { setHealth(await client.getMcpHealth(mcp.http_path ? `${mcp.http_path}/health` : '/mcp/http/health')); } catch {}
      } else {
        setHealth(null);
      }
    } catch (e: any) {
      setError(e?.message || 'Failed to load MCP config');
    }
  };

  useEffect(() => { load(); }, []);

  const apply = async () => {
    setLoading(true); setError(null); setSnack(null);
    try {
      const payload: any = {
        enable_mcp_sse: sseEnabled,
        require_mcp_oauth: requireOAuth,
        oauth_issuer: issuer || undefined,
        oauth_audience: audience || undefined,
        oauth_jwks_url: jwks || undefined,
        enable_mcp_http: httpEnabled,
      };
      const res = await client.updateSettings(payload);
      await client.reloadSettings();
      setSnack('MCP settings applied');
      if (res && res._meta && res._meta.restart_recommended) {
        setSnack('MCP settings applied. Restart recommended to take full effect.');
      }
      await load();
    } catch (e: any) {
      setError(e?.message || 'Failed to apply MCP settings');
    } finally { setLoading(false); }
  };

  const sseUrl = () => `${window.location.origin}/mcp/sse`;
  const httpUrl = () => `${window.location.origin}/mcp/http`;

  const generateClaudeConfig = () => {
    const cfg: any = {
      mcpServers: {
        faxbot: {
          transport: 'sse',
          url: sseUrl(),
        }
      }
    };
    if (!requireOAuth) {
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
          <Chip label="SSE Healthy" color="success" variant="outlined" />
        ) : (
          <Chip label="SSE Disabled/Unhealthy" color="warning" variant="outlined" />
        )}
      </Box>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {snack && <Alert severity="success" sx={{ mb: 2 }} onClose={() => setSnack(null)}>{snack}</Alert>}

      <Grid container spacing={3}>
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Server Settings</Typography>
              <Alert severity="info" sx={{ mb: 2 }}>
                Embedded Python MCP servers are mounted by the Faxbot API at /mcp/* when enabled. No external Node process is required for SSE/HTTP.
              </Alert>
              <FormControlLabel control={<Switch checked={sseEnabled} onChange={(e) => setSseEnabled(e.target.checked)} />} label="Enable SSE (/mcp/sse)" />
              <FormControlLabel control={<Switch checked={httpEnabled} onChange={(e) => setHttpEnabled(e.target.checked)} />} label="Enable Streamable HTTP (/mcp/http)" />
              <FormControlLabel control={<Switch checked={requireOAuth} onChange={(e) => setRequireOAuth(e.target.checked)} />} label="Require OAuth (JWT)" />
              {requireOAuth && (
                <Box sx={{ display: 'grid', gridTemplateColumns: '1fr', gap: 2, mt: 2 }}>
                  <TextField label="Issuer (OAUTH_ISSUER)" value={issuer} onChange={(e) => setIssuer(e.target.value)} fullWidth size="small" />
                  <TextField label="Audience (OAUTH_AUDIENCE)" value={audience} onChange={(e) => setAudience(e.target.value)} fullWidth size="small" />
                  <TextField label="JWKS URL (OAUTH_JWKS_URL)" value={jwks} onChange={(e) => setJwks(e.target.value)} fullWidth size="small" />
                </Box>
              )}
              <Box sx={{ mt: 2, display: 'flex', gap: 1 }}>
                <Button variant="contained" onClick={apply} disabled={loading}>Apply & Reload</Button>
                <Button variant="outlined" onClick={load} disabled={loading}>Refresh</Button>
              </Box>
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Claude Desktop Config</Typography>
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
              {httpEnabled && (
                <>
                  <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>HTTP Transport (experimental)</Typography>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography variant="body2" color="text.secondary">HTTP URL: {httpUrl()}</Typography>
                    <Tooltip title="Copy HTTP URL"><IconButton size="small" onClick={() => navigator.clipboard.writeText(httpUrl())}><ContentCopy fontSize="small" /></IconButton></Tooltip>
                  </Box>
                </>
              )}
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
