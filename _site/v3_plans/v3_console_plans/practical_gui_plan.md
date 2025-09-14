# Practical V3 Admin Console Implementation Plan

**For Junior Devs Working on Production Code**

## Reality Check

### What Already EXISTS (Stop Reinventing!)
- ✅ Admin UI with working tabs: Dashboard, Send, Jobs, Inbox, Keys, Setup, Settings, MCP, Logs, Diagnostics, Plugins
- ✅ API endpoints for most admin operations
- ✅ Basic plugin listing and registry viewing
- ✅ Settings export/import functionality
- ✅ API key management UI
- ✅ Job viewing and PDF downloads
- ✅ Health checks and diagnostics

### What's MISSING (Actually Build This)
- ❌ Verify feature flag toggles end‑to‑end (already present in UI)
- ❌ Plugin configuration forms
- ❌ Plugin builder UI (Node.js and Python)
- ❌ MCP status visualization polish
- ❌ Curated registry text search
- ❌ Meaningful dashboard metrics

## Implementation Phases (Simple to Complex)

---

## Phase 1: Feature Flags verification & wiring (1–2 hours) — Status: Implemented

Goal: Verify existing toggles and wire any missing backend updates.

- Settings already exposes toggles for `v3_plugins`, `fax_disabled`, `inbound_enabled`, and `plugin_install`.
- Backend returns these under `/admin/settings` and exports them in `/admin/settings/export`.
- Wire missing updates in `PUT /admin/settings` for `FEATURE_V3_PLUGINS` and `FEATURE_PLUGIN_INSTALL`, and ensure Settings.tsx includes them when applying.
- Toggle, Apply & Reload, then confirm values and restart hint.

Smoke test:
```bash
cd api && ENABLE_LOCAL_ADMIN=true python -m app.main
curl -s -H "X-API-Key: $API_KEY" http://localhost:8080/admin/settings | jq '.features'
```

---

## Phase 2: Fix Plugin Management UI (3 hours) — Status: Implemented

**Goal**: Make the existing Plugins tab actually useful

### Step 2.1: Add Plugin Configuration Dialog

```typescript
// Create new file: api/admin_ui/src/components/PluginConfigDialog.tsx

import { useState } from 'react';
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Button,
  Alert,
  Box,
  Typography
} from '@mui/material';
import SecretInput from './common/SecretInput';

interface Props {
  open: boolean;
  plugin: any;
  onClose: () => void;
  onSave: (config: any) => Promise<void>;
}

export default function PluginConfigDialog({ open, plugin, onClose, onSave }: Props) {
  const [config, setConfig] = useState<any>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  // Simple form based on plugin type
  const renderForm = () => {
    if (!plugin) return null;
    
    if (plugin.id === 'phaxio') {
      return (
        <>
          <Alert severity="info" sx={{ mb: 2 }}>
            Secrets (API key/secret) are configured in Settings → Backend: Phaxio. This plugin config stores only non‑secret values.
          </Alert>
          <TextField
            label="Callback URL"
            value={config.callback_url || ''}
            onChange={(e) => setConfig({...config, callback_url: e.target.value})}
            fullWidth
            margin="normal"
            helperText="https://yourdomain.com/phaxio-callback"
          />
        </>
      );
    }
    
    if (plugin.id === 'sip') {
      return (
        <>
          <Alert severity="info">
            Configure AMI host/port/credentials and Station ID in Settings → Backend: SIP/Asterisk. Plugin config should not include secrets.
          </Alert>
        </>
      );
    }
    
    // Generic form for unknown plugins
    return (
      <Alert severity="info">
        Configuration for this plugin type coming soon
      </Alert>
    );
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      setError('');
      await onSave(config);
      onClose();
    } catch (e: any) {
      setError(e.message || 'Failed to save configuration');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Configure {plugin?.name}</DialogTitle>
      <DialogContent>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Enter configuration for this provider. Settings are saved to config file.
        </Typography>
        {renderForm()}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button 
          onClick={handleSave} 
          variant="contained" 
          disabled={saving}
        >
          {saving ? 'Saving...' : 'Save Configuration'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
```

### Step 2.2: Add Configure Button to Plugin Cards

```typescript
// In api/admin_ui/src/components/Plugins.tsx
// MODIFY the CardActions section around line 132

<CardActions>
  <Button 
    size="small" 
    onClick={() => handleConfigure(p)}
  >
    Configure
  </Button>
  {onActivate && (
    <Button 
      size="small" 
      variant="contained" 
      disabled={saving === p.id} 
      onClick={() => onActivate(p.id)}
    >
      {saving === p.id ? 'Saving…' : 'Set Active'}
    </Button>
  )}
</CardActions>
```

### Smoke Test 2.1
```bash
# Open browser to http://localhost:8080/admin
# Navigate to Plugins tab
# Click "Configure" on any plugin
# Should see configuration dialog with appropriate fields
```

---

## Phase 3: Refine MCP Tab (2 hours)

**Goal**: Make MCP tab show actual status and provide simple configuration

Note: Keep the existing `MCP.tsx` component. Refine copy and add copy‑to‑clipboard helpers. The Admin API embeds the Python MCP transports at `/mcp/sse` and `/mcp/http` when enabled; health should be fetched from those paths. Do not attempt to manage external Node MCP processes from the Admin Console; the Node servers under `node_mcp/` remain available for parity and external use.

### Step 3.1: Simplify MCP Component

```typescript
// REPLACE the entire faxbot/api/admin_ui/src/components/MCP.tsx

import { useEffect, useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Alert,
  Grid,
  Chip,
  Button,
  List,
  ListItem,
  ListItemText,
  ListItemIcon,
  Paper,
  TextField,
  IconButton,
  Tooltip
} from '@mui/material';
import {
  CheckCircle,
  Error,
  Warning,
  ContentCopy,
  Refresh
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';

interface Props {
  client: AdminAPIClient;
}

interface MCPServer {
  name: string;
  transport: string;
  port?: number;
  status: 'running' | 'stopped' | 'error';
  config?: string;
  tools?: string[];
}

export default function MCP({ client }: Props) {
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>('');

  const checkServers = async () => {
    setLoading(true);
    setError('');
    
    const serverConfigs: MCPServer[] = [
      { name: 'Node Stdio', transport: 'stdio', status: 'stopped' },
      { name: 'Node HTTP', transport: 'http', port: 3001, status: 'stopped' },
      { name: 'Node SSE', transport: 'sse', port: 3002, status: 'stopped' },
      { name: 'Python Stdio', transport: 'stdio', status: 'stopped' },
      { name: 'Python SSE', transport: 'sse', port: 3003, status: 'stopped' }
    ];

    // Check each server's health
    for (const server of serverConfigs) {
      if (server.port) {
        try {
          await client.getMcpHealth(`/mcp/${server.transport}/health`);
          server.status = 'running';
        } catch {
          server.status = 'stopped';
        }
      }
    }

    setServers(serverConfigs);
    setLoading(false);
  };

  useEffect(() => {
    checkServers();
  }, []);

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'running':
        return <CheckCircle color="success" />;
      case 'error':
        return <Error color="error" />;
      default:
        return <Warning color="warning" />;
    }
  };

  const getConfigForTransport = (transport: string, port?: number) => {
    const baseUrl = window.location.origin.replace(/:\d+$/, '');
    
    if (transport === 'stdio') {
      return JSON.stringify({
        "mcpServers": {
          "faxbot": {
            "command": "node",
            "args": ["node_mcp/src/servers/stdio.js"],
            "env": {
              "FAX_API_URL": `${baseUrl}:8080`,
              "API_KEY": "your-api-key"
            }
          }
        }
      }, null, 2);
    }
    
    if (transport === 'sse') {
      return `${window.location.origin}/mcp/sse`;
    }
    if (transport === 'http') {
      return `${window.location.origin}/mcp/http`;
    }
    
    return '';
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4">MCP Servers</Typography>
        <Button
          variant="outlined"
          startIcon={<Refresh />}
          onClick={checkServers}
          disabled={loading}
        >
          Refresh Status
        </Button>
      </Box>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      <Alert severity="info" sx={{ mb: 3 }}>
        MCP servers enable AI assistants like Claude to interact with Faxbot. 
        Each transport type serves different use cases.
      </Alert>

      <Grid container spacing={2}>
        {servers.map((server) => (
          <Grid item xs={12} md={6} key={`${server.name}-${server.transport}`}>
            <Card>
              <CardContent>
                <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
                  <Typography variant="h6">{server.name}</Typography>
                  <Box display="flex" alignItems="center" gap={1}>
                    {getStatusIcon(server.status)}
                    <Chip 
                      label={server.status} 
                      color={server.status === 'running' ? 'success' : 'default'}
                      size="small"
                    />
                  </Box>
                </Box>

                <List dense>
                  <ListItem>
                    <ListItemText 
                      primary="Transport"
                      secondary={server.transport.toUpperCase()}
                    />
                  </ListItem>
                  {server.port && (
                    <ListItem>
                      <ListItemText 
                        primary="Port"
                        secondary={server.port}
                      />
                    </ListItem>
                  )}
                </List>

                {server.transport === 'stdio' && (
                  <Box mt={2}>
                    <Typography variant="body2" color="text.secondary" gutterBottom>
                      Claude Desktop Configuration:
                    </Typography>
                    <Paper sx={{ p: 1, bgcolor: 'background.default' }}>
                      <pre style={{ margin: 0, fontSize: '0.75rem', overflow: 'auto' }}>
                        {getConfigForTransport(server.transport)}
                      </pre>
                      <Tooltip title="Copy configuration">
                        <IconButton 
                          size="small" 
                          onClick={() => copyToClipboard(getConfigForTransport(server.transport))}
                          sx={{ position: 'absolute', right: 8, top: 8 }}
                        >
                          <ContentCopy fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </Paper>
                  </Box>
                )}

                {(server.transport === 'http' || server.transport === 'sse') && (
                  <Box mt={2}>
                    <Typography variant="body2" color="text.secondary" gutterBottom>
                      Endpoint URL:
                    </Typography>
                    <TextField
                      value={getConfigForTransport(server.transport, server.port)}
                      fullWidth
                      size="small"
                      InputProps={{
                        readOnly: true,
                        endAdornment: (
                          <IconButton 
                            size="small"
                            onClick={() => copyToClipboard(getConfigForTransport(server.transport, server.port))}
                          >
                            <ContentCopy fontSize="small" />
                          </IconButton>
                        )
                      }}
                    />
                  </Box>
                )}
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      <Card sx={{ mt: 3 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>Quick Start</Typography>
          <Typography variant="body2" paragraph>
            1. Choose your transport type (stdio for Claude Desktop, HTTP/SSE for web)
          </Typography>
          <Typography variant="body2" paragraph>
            2. Copy the configuration above
          </Typography>
          <Typography variant="body2" paragraph>
            3. For stdio: Add to Claude Desktop settings
          </Typography>
          <Typography variant="body2" paragraph>
            4. For HTTP/SSE: Use the endpoint URL in your application
          </Typography>
        </CardContent>
      </Card>
    </Box>
  );
}
```

### Smoke Test 3.1
```bash
cd api && ENABLE_LOCAL_ADMIN=true ENABLE_MCP_SSE=true python -m app.main
curl -s http://localhost:8080/mcp/sse/health | jq
```

---

## Phase 4: Basic Plugin Builder (6 hours) — Status: Implemented

**Goal**: Create simple form-based plugin builder for SIP providers (most common use case)

### Step 4.1: Create Plugin Builder Component

```typescript
// Create new file: api/admin_ui/src/components/PluginBuilder.tsx

import { useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  TextField,
  Button,
  Stepper,
  Step,
  StepLabel,
  Alert,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  Paper,
  FormGroup,
  FormControlLabel,
  Checkbox
} from '@mui/material';
import AdminAPIClient from '../api/client';

interface Props {
  client: AdminAPIClient;
}

const steps = ['Basic Info', 'Provider Settings', 'Capabilities', 'Review & Generate'];

export default function PluginBuilder({ client }: Props) {
  const [activeStep, setActiveStep] = useState(0);
  const [pluginData, setPluginData] = useState({
    name: '',
    id: '',
    version: '1.0.0',
    sdk: 'python',
    type: 'fax',
    provider: 'sip',
    // SIP specific
    sipTrunk: '',
    t38Support: true,
    // Capabilities
    capabilities: ['send'],
    // Generated code
    generatedCode: ''
  });

  const handleNext = () => {
    if (activeStep === steps.length - 1) {
      generatePlugin();
    } else {
      setActiveStep((prev) => prev + 1);
    }
  };

  const handleBack = () => {
    setActiveStep((prev) => prev - 1);
  };

  const generatePlugin = () => {
    // Generate the actual plugin code
    const code = generatePluginCode(pluginData);
    setPluginData({ ...pluginData, generatedCode: code });
  };

  const generatePluginCode = (data: any) => {
    if (data.sdk === 'python') {
      return `\"\"\"
${data.name} - Faxbot Plugin for ${data.provider.toUpperCase()}
Version: ${data.version}
\"\"\"

from faxbot_plugin_dev import FaxPlugin, SendResult, StatusResult, PluginDeps, FaxStatus
from typing import Optional
import httpx

class ${data.id.replace(/-/g, '_').replace(/^/, '').split('_').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join('')}Plugin(FaxPlugin):
    \"\"\"${data.name} implementation\"\"\"
    
    def __init__(self, deps: PluginDeps):
        super().__init__(deps)
        self.sip_trunk = "${data.sipTrunk}"
        self.t38_enabled = ${data.t38Support}
    
    async def send_fax(
        self,
        to_number: str,
        file_path: str,
        from_number: Optional[str] = None
    ) -> SendResult:
        \"\"\"Send fax via ${data.provider}\"\"\"
        # TODO: Implement SIP/T.38 transmission
        # 1. Convert PDF to TIFF
        # 2. Initiate SIP call
        # 3. Negotiate T.38
        # 4. Transmit fax
        
        return SendResult(
            job_id="test-job-id",
            backend="${data.provider}",
            provider_sid="provider-job-123",
            metadata={"note": "queued"}
        )
    
    async def get_status(self, job_id: str) -> StatusResult:
        \"\"\"Check fax status\"\"\"
        return StatusResult(
            job_id=job_id,
            status=FaxStatus.SUCCESS,
            pages=1
        )

# Plugin manifest
MANIFEST = {
    "id": "${data.id}",
    "name": "${data.name}",
    "version": "${data.version}",
    "categories": ["outbound"],
    "capabilities": ${JSON.stringify(data.capabilities)},
    "config_schema": {
        "type": "object",
        "properties": {
            "sip_trunk": {"type": "string"},
            "username": {"type": "string"},
            "password": {"type": "string"}
        }
    }
}`;
    } else {
      // Node.js version
      return `/**
 * ${data.name} - Faxbot Plugin for ${data.provider.toUpperCase()}
 * Version: ${data.version}
 */

const { FaxPlugin } = require('@faxbot/plugin-dev');

class ${data.id.replace(/-/g, '').split(/(?=[A-Z])/).map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join('')}Plugin extends FaxPlugin {
  constructor(deps) {
    super(deps);
    this.sipTrunk = '${data.sipTrunk}';
    this.t38Enabled = ${data.t38Support};
  }

  async sendFax(toNumber, filePath, options = {}) {
    // TODO: Implement SIP/T.38 transmission
    return {
      jobId: 'test-job-id',
      backend: '${data.provider}',
      providerSid: 'provider-job-123',
      metadata: { note: 'queued' }
    };
  }

  async getStatus(jobId) {
    return {
      jobId,
      status: 'SUCCESS',
      pages: 1
    };
  }
}

module.exports = {
  Plugin: ${data.id.replace(/-/g, '').split(/(?=[A-Z])/).map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join('')}Plugin,
  manifest: {
    id: '${data.id}',
    name: '${data.name}',
    version: '${data.version}',
    categories: ['outbound'],
    capabilities: ${JSON.stringify(data.capabilities)}
  }
};`;
    }
  };

  const renderStepContent = () => {
    switch (activeStep) {
      case 0:
        return (
          <Box>
            <TextField
              label="Plugin Name"
              value={pluginData.name}
              onChange={(e) => setPluginData({ ...pluginData, name: e.target.value })}
              fullWidth
              margin="normal"
              helperText="Human-readable name for your plugin"
            />
            <TextField
              label="Plugin ID"
              value={pluginData.id}
              onChange={(e) => setPluginData({ ...pluginData, id: e.target.value })}
              fullWidth
              margin="normal"
              helperText="Unique identifier (lowercase, hyphens)"
            />
            <FormControl fullWidth margin="normal">
              <InputLabel>SDK</InputLabel>
              <Select
                value={pluginData.sdk}
                onChange={(e) => setPluginData({ ...pluginData, sdk: e.target.value })}
              >
                <MenuItem value="python">Python</MenuItem>
                <MenuItem value="node">Node.js</MenuItem>
              </Select>
            </FormControl>
          </Box>
        );
      
      case 1:
        return (
          <Box>
            <Alert severity="info" sx={{ mb: 2 }}>
              SIP provider configuration for T.38 fax transmission
            </Alert>
            <TextField
              label="SIP Trunk Provider"
              value={pluginData.sipTrunk}
              onChange={(e) => setPluginData({ ...pluginData, sipTrunk: e.target.value })}
              fullWidth
              margin="normal"
              helperText="e.g., Twilio, Bandwidth, Voxbone"
            />
            <FormControlLabel
              control={
                <Checkbox
                  checked={pluginData.t38Support}
                  onChange={(e) => setPluginData({ ...pluginData, t38Support: e.target.checked })}
                />
              }
              label="T.38 Support Required"
            />
          </Box>
        );
      
      case 2:
        return (
          <Box>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Select the capabilities your plugin will provide
            </Typography>
            <FormGroup>
              <FormControlLabel
                control={<Checkbox checked disabled />}
                label="Send Fax (always included)"
              />
              <FormControlLabel
                control={
                  <Checkbox
                    checked={pluginData.capabilities.includes('get_status')}
                    onChange={(e) => {
                      const caps = e.target.checked 
                        ? [...pluginData.capabilities, 'get_status']
                        : pluginData.capabilities.filter(c => c !== 'get_status');
                      setPluginData({ ...pluginData, capabilities: caps });
                    }}
                  />
                }
                label="Get Status"
              />
            </FormGroup>
          </Box>
        );
      
      case 3:
        return (
          <Box>
            <Alert severity="success" sx={{ mb: 2 }}>
              Plugin code generated! Copy the code below and save as a new file.
            </Alert>
            <Typography variant="body2" sx={{ mb: 1 }}>
              Save as: <code>{pluginData.id}.{pluginData.sdk === 'python' ? 'py' : 'js'}</code>
            </Typography>
            <Paper sx={{ p: 2, bgcolor: 'background.default', maxHeight: 400, overflow: 'auto' }}>
              <pre style={{ margin: 0, fontSize: '0.8rem' }}>
                {pluginData.generatedCode || 'Click "Generate" to create plugin code'}
              </pre>
            </Paper>
            <Button
              variant="contained"
              sx={{ mt: 2 }}
              onClick={() => {
                const blob = new Blob([pluginData.generatedCode], { type: 'text/plain' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${pluginData.id}.${pluginData.sdk === 'python' ? 'py' : 'js'}`;
                a.click();
              }}
            >
              Download Plugin File
            </Button>
          </Box>
        );
      
      default:
        return null;
    }
  };

  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        Plugin Builder
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Create a new Faxbot plugin for SIP/T.38 providers
      </Typography>

      <Stepper activeStep={activeStep} sx={{ mb: 3 }}>
        {steps.map((label) => (
          <Step key={label}>
            <StepLabel>{label}</StepLabel>
          </Step>
        ))}
      </Stepper>

      <Card>
        <CardContent>
          {renderStepContent()}
          
          <Box display="flex" justifyContent="space-between" sx={{ mt: 3 }}>
            <Button
              disabled={activeStep === 0}
              onClick={handleBack}
            >
              Back
            </Button>
            <Button
              variant="contained"
              onClick={handleNext}
            >
              {activeStep === steps.length - 1 ? 'Generate' : 'Next'}
            </Button>
          </Box>
        </CardContent>
      </Card>
    </Box>
  );
}
```

### Step 4.2: Add Plugin Builder Tab to App

```typescript
// In api/admin_ui/src/App.tsx
// ADD import after line 29
import PluginBuilder from './components/PluginBuilder';

// ADD tab after line 220 (after Plugins tab)
<Tab label="Plugin Builder" />

// ADD TabPanel after the Plugins TabPanel
<TabPanel value={tabValue} index={/* next index */}>
  <PluginBuilder client={apiClient} />
</TabPanel>
```

### Smoke Test 4.1
```bash
# Navigate to Plugin Builder tab
# Fill out the form step by step
# Should generate working plugin code
# Download button should save file
```

---

## Phase 5: Expanded Dashboard for Devs (3 hours) — Status: Implemented

**Goal**: Make the dashboard show actual useful information

### Step 5.1: Update Dashboard Component

```typescript
// MODIFY api/admin_ui/src/components/Dashboard.tsx
// This is a focused update, not a full rewrite

// Add these metric cards after the existing content
const metrics = [
  { label: 'Total Faxes Sent', value: stats.total_sent || 0, color: 'primary' },
  { label: 'Success Rate', value: `${stats.success_rate || 0}%`, color: 'success' },
  { label: 'Active Backend', value: settings?.backend?.type || 'Unknown', color: 'info' },
  { label: 'API Keys', value: apiKeys.length, color: 'warning' }
];

// In the render section, add:
<Grid container spacing={2} sx={{ mb: 3 }}>
  {metrics.map((metric) => (
    <Grid item xs={12} sm={6} md={3} key={metric.label}>
      <Card>
        <CardContent>
          <Typography color="text.secondary" variant="body2">
            {metric.label}
          </Typography>
          <Typography variant="h4" color={`${metric.color}.main`}>
            {metric.value}
          </Typography>
        </CardContent>
      </Card>
    </Grid>
  ))}
</Grid>
```

### Step 5.2: Add Config Overview card

Goal: Show static posture from `/admin/config` without heavy queries.

Suggested fields:
- Backend: `cfg.backend`
- Storage: `cfg.storage.backend`
- Security: `cfg.require_api_key`, `cfg.enforce_public_https`, `cfg.audit_log_enabled`
- v3 Plugins: `cfg.v3_plugins?.enabled`, `cfg.v3_plugins?.active_outbound`

Implementation notes:
- Fetch `cfg` via `client.getConfig()` on mount and during existing poll.
- Render a compact card with key–value rows and small chips (Enabled/Disabled).

### Step 5.3: Add MCP Overview card

Goal: Show which MCP transports are enabled and where.

Suggested fields:
- SSE: `cfg.mcp?.sse_enabled` at `cfg.mcp?.sse_path`
- HTTP: `cfg.mcp?.http_enabled` at `cfg.mcp?.http_path`
- OAuth required: `cfg.mcp?.require_oauth`

Implementation notes:
- Add copy buttons for `${window.location.origin}${cfg.mcp?.sse_path}` and HTTP path.
- Keep health pings optional; config‑only view is acceptable.

### Step 5.4: Add Plugins card (feature‑gated)

Goal: Quick visibility into plugin state when `FEATURE_V3_PLUGINS=true`.

Suggested fields:
- Active outbound: `cfg.v3_plugins?.active_outbound`
- Installed count: from `client.listPlugins()` length

Implementation notes:
- Load plugins list only when `cfg.v3_plugins?.enabled` is true.
- Show a small list of top 3 with “View all” linking to Plugins tab.

### Step 5.5: Add SDK & Quickstart card

Goal: Make it obvious “what to run” during development.

Contents:
- Base URL: `window.location.origin`
- Auth header: `X-API-Key: <your key>`
- Node SDK: `npm i faxbot@1.0.2`
- Python SDK: `pip install faxbot==1.0.2`
- Minimal send/status snippets for Node and Python, copy‑to‑clipboard buttons.

Keep existing health/job counters; avoid heavy queries or server‑side metrics.

---

## Phase 6: Curated Registry Search (2 hours) — Status: Implemented

Goal: Provide a lightweight search over the curated registry (no remote install).

Implementation:
- Added a search box to Plugins tab that filters installed and registry entries.
- Added a Discover (Curated Registry) section that lists registry items not installed, with links to “Learn more”.
- Remote install remains disabled; UI shows “Install Disabled” when no link is provided.

---

## Phase 7: Polish & HIPAA Compliance (3 hours)

**Goal**: Add PHI warnings and final touches

### Step 7.1: Add HIPAA Warnings Where Needed

```typescript
// Create new file: api/admin_ui/src/components/common/HIPAAWarning.tsx

import { Alert, AlertTitle } from '@mui/material';

interface Props {
  context: string;
}

export function HIPAAWarning({ context }: Props) {
  return (
    <Alert severity="warning" sx={{ mb: 2 }}>
      <AlertTitle>HIPAA Notice</AlertTitle>
      {context === 'logs' && 
        'Logs may contain PHI. Ensure proper access controls and retention policies.'}
      {context === 'jobs' && 
        'Job data contains phone numbers and may reference PHI documents.'}
      {context === 'inbound' && 
        'Inbound faxes likely contain PHI. Download only when necessary.'}
      {context === 'settings' && 
        'Changing security settings may affect HIPAA compliance.'}
    </Alert>
  );
}
```

### Step 7.2: Add to Relevant Components

```typescript
// In JobsList, Logs, Inbound, Settings components
// ADD at the top of the render:
import { HIPAAWarning } from './common/HIPAAWarning';

// Then in the JSX:
<HIPAAWarning context="jobs" />
```

---

## Testing Everything (1 hour)

### Full Integration Test

```bash
# 1. Start the backend
cd api
python -m app.main

# 2. Build and serve the admin UI
cd api/admin_ui
npm install
npm run build
# The API serves the built files automatically

# 3. Open browser to http://localhost:8080/admin

# 4. Test each phase:
# - Toggle feature flags in Settings
# - Configure a plugin
# - Check MCP status
# - Build a test plugin
# - View dashboard metrics
# - Search for plugins
# - Verify HIPAA warnings appear
```

---

## Rollback Plan

If anything breaks in production:

1. **Quick Fix**: Revert the specific component file
2. **Full Rollback**: `git checkout HEAD~1 -- api/admin_ui/`
3. **Rebuild**: `cd api/admin_ui && npm run build`

---

## What We're NOT Building

- ❌ Docker container management (that's Portainer)
- ❌ Full plugin marketplace (against FOSS principles)
- ❌ Complex monitoring dashboards (use Grafana)
- ❌ Log aggregation (use ELK stack)
- ❌ User management system (out of scope)

---

## Success Criteria

✅ User can enable v3 features without touching terminal
✅ User can see and configure plugins via GUI
✅ User can build a basic SIP plugin via form
✅ MCP configuration is copy-paste simple
✅ Dashboard shows real, useful metrics
✅ HIPAA warnings prevent accidental PHI exposure

---

## Notes for Implementers

1. **Start with Phase 1** - It's the simplest and most valuable
2. **Test after each phase** - Don't wait until the end
3. **Keep existing functionality** - We're enhancing, not replacing
4. **When in doubt, keep it simple** - This is for production use
5. **Document any API changes** - Update OpenAPI spec if needed

Remember: The goal is to make everything accessible via GUI while keeping the system stable and HIPAA-compliant. Every user interaction should be possible through the browser after `docker compose up`.
