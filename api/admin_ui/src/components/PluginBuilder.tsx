import { useState } from 'react';
import { Box, Card, CardContent, Typography, TextField, Button, Stepper, Step, StepLabel, Alert, FormControl, InputLabel, Select, MenuItem, Paper, FormGroup, FormControlLabel, Checkbox } from '@mui/material';
import type AdminAPIClient from '../api/client';

interface Props { client: AdminAPIClient }

const steps = ['Basic Info', 'Provider Settings', 'Capabilities', 'Review & Generate'];

export default function PluginBuilder({}: Props) {
  const [activeStep, setActiveStep] = useState(0);
  const [pluginData, setPluginData] = useState<any>({
    name: '',
    id: '',
    version: '1.0.0',
    sdk: 'python', // 'python' | 'node'
    provider: 'sip',
    sipTrunk: '',
    t38Support: true,
    capabilities: ['send', 'get_status'],
    generatedCode: ''
  });

  const next = () => {
    if (activeStep === steps.length - 1) generate(); else setActiveStep((s) => s + 1);
  };
  const back = () => setActiveStep((s) => Math.max(0, s - 1));

  const generate = () => {
    const code = gen(pluginData);
    setPluginData({ ...pluginData, generatedCode: code });
  };

  const toPyClass = (id: string) => id.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join('') + 'Plugin';
  const toJsClass = (id: string) => id.replace(/[^a-zA-Z0-9]+/g, ' ').split(' ').filter(Boolean).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join('') + 'Plugin';

  const gen = (d: any) => {
    if (d.sdk === 'python') {
      return `"""
${d.name} - Faxbot Plugin (${d.provider.toUpperCase()})
Version: ${d.version}
"""
from faxbot_plugin_dev import FaxPlugin, SendResult, StatusResult, PluginDeps, FaxStatus
from typing import Optional, Dict, Any

class ${toPyClass(d.id)}(FaxPlugin):
    def __init__(self, deps: PluginDeps):
        super().__init__(deps)
        self.sip_trunk = "${d.sipTrunk}"
        self.t38_enabled = ${d.t38Support}

    async def send_fax(self, to_number: str, file_path: str, options: Optional[Dict[str, Any]] = None) -> SendResult:
        # TODO: Implement SIP/T.38 transmission
        return SendResult(job_id="test-job-id", backend="${d.provider}", provider_sid="provider-job-123", metadata={"note": "queued"})

    async def get_status(self, job_id: str) -> StatusResult:
        return StatusResult(job_id=job_id, status=FaxStatus.SUCCESS, pages=1)

MANIFEST = {
    "id": "${d.id}",
    "name": "${d.name}",
    "version": "${d.version}",
    "categories": ["outbound"],
    "capabilities": ${JSON.stringify(d.capabilities)},
    "config_schema": {
        "type": "object",
        "properties": {"sip_trunk": {"type": "string"}, "username": {"type": "string"}, "password": {"type": "string"}}
    }
}
`;
    }
    // Node
    return `/**
 * ${d.name} - Faxbot Plugin (${d.provider.toUpperCase()})
 * Version: ${d.version}
 */
const { FaxPlugin } = require('@faxbot/plugin-dev');

class ${toJsClass(d.id)} extends FaxPlugin {
  constructor(deps) { super(deps); this.sipTrunk='${d.sipTrunk}'; this.t38Enabled=${d.t38Support}; }
  async sendFax(toNumber, filePath, options = {}) { return { jobId: 'test-job-id', backend: '${d.provider}', providerSid: 'provider-job-123', metadata: { note: 'queued' } }; }
  async getStatus(jobId) { return { jobId, status: 'SUCCESS', pages: 1 }; }
}

module.exports = { Plugin: ${toJsClass(d.id)}, manifest: { id: '${d.id}', name: '${d.name}', version: '${d.version}', categories: ['outbound'], capabilities: ${JSON.stringify(d.capabilities)} } };
`;
  };

  const render = () => {
    switch (activeStep) {
      case 0:
        return (
          <Box>
            <TextField label="Plugin Name" value={pluginData.name} onChange={(e)=>setPluginData({...pluginData, name: e.target.value})} fullWidth margin="normal" />
            <TextField label="Plugin ID" value={pluginData.id} onChange={(e)=>setPluginData({...pluginData, id: e.target.value})} fullWidth margin="normal" helperText="lowercase-with-hyphens" />
            <FormControl fullWidth margin="normal" size="small">
              <InputLabel>SDK</InputLabel>
              <Select value={pluginData.sdk} label="SDK" onChange={(e)=>setPluginData({...pluginData, sdk: e.target.value})}>
                <MenuItem value="python">Python</MenuItem>
                <MenuItem value="node">Node.js</MenuItem>
              </Select>
            </FormControl>
          </Box>
        );
      case 1:
        return (
          <Box>
            <Alert severity="info" sx={{ mb: 2 }}>SIP provider inputs (non‑secret)</Alert>
            <TextField label="SIP Trunk Provider" value={pluginData.sipTrunk} onChange={(e)=>setPluginData({...pluginData, sipTrunk: e.target.value})} fullWidth margin="normal" />
            <FormControlLabel control={<Checkbox checked={pluginData.t38Support} onChange={(e)=>setPluginData({...pluginData, t38Support: e.target.checked})} />} label="T.38 Support Required" />
          </Box>
        );
      case 2:
        return (
          <Box>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Select capabilities</Typography>
            <FormGroup>
              <FormControlLabel control={<Checkbox checked disabled />} label="Send Fax" />
              <FormControlLabel control={<Checkbox checked={pluginData.capabilities.includes('get_status')} onChange={(e)=>{
                const cap = 'get_status';
                setPluginData({
                  ...pluginData,
                  capabilities: e.target.checked ? [...pluginData.capabilities, cap] : pluginData.capabilities.filter((c:string)=>c!==cap)
                });
              }} />} label="Get Status" />
            </FormGroup>
          </Box>
        );
      case 3:
        return (
          <Box>
            <Alert severity="success" sx={{ mb: 2 }}>Plugin code generated! Copy or download the file.</Alert>
            <Typography variant="body2" sx={{ mb: 1 }}>Save as: <code>{pluginData.id}.{pluginData.sdk === 'python' ? 'py' : 'js'}</code></Typography>
            <Paper sx={{ p: 2, bgcolor: 'background.default', maxHeight: 420, overflow: 'auto' }}>
              <pre style={{ margin: 0, fontSize: '0.8rem' }}>{pluginData.generatedCode || 'Click "Generate" to create plugin code'}</pre>
            </Paper>
            <Button variant="contained" sx={{ mt: 2 }} onClick={() => {
              const blob = new Blob([pluginData.generatedCode], { type: 'text/plain' });
              const url = URL.createObjectURL(blob); const a = document.createElement('a');
              a.href = url; a.download = `${pluginData.id}.${pluginData.sdk === 'python' ? 'py' : 'js'}`; a.click();
            }}>Download Plugin File</Button>
          </Box>
        );
      default:
        return null;
    }
  };

  return (
    <Box>
      <Typography variant="h4" gutterBottom>Plugin Builder</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Create a new Faxbot plugin (Python or Node)</Typography>

      <Stepper activeStep={activeStep} sx={{ mb: 3 }}>
        {steps.map((label) => (<Step key={label}><StepLabel>{label}</StepLabel></Step>))}
      </Stepper>

      <Card>
        <CardContent>
          {render()}
          <Box display="flex" justifyContent="space-between" sx={{ mt: 3 }}>
            <Button disabled={activeStep === 0} onClick={back}>Back</Button>
            <Button variant="contained" onClick={next}>{activeStep === steps.length - 1 ? 'Generate' : 'Next'}</Button>
          </Box>
        </CardContent>
      </Card>
    </Box>
  );
}
