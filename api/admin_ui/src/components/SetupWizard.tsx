import { useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Stepper,
  Step,
  StepLabel,
  Button,
  TextField,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  Alert,
  CircularProgress,
  Grid,
  Paper,
} from '@mui/material';
import { Chip } from '@mui/material';
import AdminAPIClient from '../api/client';
import SecretInput from './common/SecretInput';

interface SetupWizardProps {
  client: AdminAPIClient;
  onDone?: () => void;
  docsBase?: string;
}

interface WizardConfig {
  backend: string;
  phaxio_api_key?: string;
  phaxio_api_secret?: string;
  public_api_url?: string;
  sinch_project_id?: string;
  sinch_api_key?: string;
  sinch_api_secret?: string;
  ami_host?: string;
  ami_port?: number;
  ami_username?: string;
  ami_password?: string;
  fax_station_id?: string;
  require_api_key?: boolean;
  enforce_public_https?: boolean;
  audit_log_enabled?: boolean;
  pdf_token_ttl_minutes?: number;
}

function SetupWizard({ client, onDone, docsBase }: SetupWizardProps) {
  const [activeStep, setActiveStep] = useState(0);
  const [config, setConfig] = useState<WizardConfig>({
    backend: 'phaxio',
    require_api_key: true,
    enforce_public_https: true,
    audit_log_enabled: false,
    pdf_token_ttl_minutes: 60,
  });
  const [validating, setValidating] = useState(false);
  const [validationResults, setValidationResults] = useState<any>(null);
  const [envContent, setEnvContent] = useState('');
  const [snack, setSnack] = useState<string | null>(null);
  const [verifyingInbound, setVerifyingInbound] = useState(false);
  const [verifyFound, setVerifyFound] = useState<any | null>(null);
  const [callbacks, setCallbacks] = useState<any | null>(null);

  const steps = ['Choose Backend', 'Configure Credentials', 'Security Settings', 'Apply & Export'];

  const handleNext = () => {
    setActiveStep((prevActiveStep) => prevActiveStep + 1);
  };

  const handleBack = () => {
    setActiveStep((prevActiveStep) => prevActiveStep - 1);
  };

  const handleConfigChange = (field: string, value: any) => {
    setConfig(prev => ({ ...prev, [field]: value }));
  };

  const handleValidate = async () => {
    setValidating(true);
    try {
      const results = await client.validateSettings(config);
      setValidationResults(results);
    } catch (e) {
      setValidationResults({ error: e instanceof Error ? e.message : 'Validation failed' });
    } finally {
      setValidating(false);
    }
  };

  const loadCallbacks = async () => {
    try { setCallbacks(await client.getInboundCallbacks()); } catch { setCallbacks(null); }
  };

  const startVerifyInbound = async () => {
    setVerifyFound(null);
    setVerifyingInbound(true);
    const since = new Date().toISOString();
    // Poll logs for inbound_received events since now
    let attempts = 0;
    const poll = async () => {
      if (!verifyingInbound || attempts++ > 30) { setVerifyingInbound(false); return; }
      try {
        const res = await client.getLogs({ event: 'inbound_received', since });
        if (res.items && res.items.length > 0) {
          setVerifyFound(res.items[0]);
          setVerifyingInbound(false);
          setSnack('Inbound verified (event received)');
          return;
        }
      } catch {}
      setTimeout(poll, 2000);
    };
    poll();
  };

  const generateEnvContent = () => {
    const lines = [];
    
    lines.push(`FAX_BACKEND=${config.backend}`);
    lines.push(`REQUIRE_API_KEY=${config.require_api_key}`);
    lines.push(`ENFORCE_PUBLIC_HTTPS=${config.enforce_public_https}`);
    lines.push(`AUDIT_LOG_ENABLED=${config.audit_log_enabled}`);
    lines.push(`PDF_TOKEN_TTL_MINUTES=${config.pdf_token_ttl_minutes}`);
    lines.push('');
    lines.push('# Backend-specific configuration');

    if (config.backend === 'phaxio') {
      lines.push('# Phaxio Configuration');
      lines.push(`PHAXIO_API_KEY=${config.phaxio_api_key || 'your_api_key_here'}`);
      lines.push(`PHAXIO_API_SECRET=${config.phaxio_api_secret || 'your_api_secret_here'}`);
      if (config.public_api_url) {
        lines.push(`PUBLIC_API_URL=${config.public_api_url}`);
        lines.push(`PHAXIO_STATUS_CALLBACK_URL=${config.public_api_url}/phaxio-callback`);
      }
      lines.push('PHAXIO_VERIFY_SIGNATURE=true');
    } else if (config.backend === 'sinch') {
      lines.push('# Sinch Fax API v3 Configuration');
      lines.push(`SINCH_PROJECT_ID=${config.sinch_project_id || 'your_project_id_here'}`);
      lines.push(`SINCH_API_KEY=${config.sinch_api_key || 'your_api_key_here'}`);
      lines.push(`SINCH_API_SECRET=${config.sinch_api_secret || 'your_api_secret_here'}`);
    } else if (config.backend === 'sip') {
      lines.push('# SIP/Asterisk Configuration');
      lines.push(`ASTERISK_AMI_HOST=${config.ami_host || 'asterisk'}`);
      lines.push(`ASTERISK_AMI_PORT=${config.ami_port || 5038}`);
      lines.push(`ASTERISK_AMI_USERNAME=${config.ami_username || 'api'}`);
      lines.push(`ASTERISK_AMI_PASSWORD=${config.ami_password || 'change_me'}`);
      lines.push(`FAX_LOCAL_STATION_ID=${config.fax_station_id || '+15551234567'}`);
    } else if (config.backend === 'documo') {
      lines.push('# Documo (mFax) Configuration');
      lines.push(`DOCUMO_API_KEY=${config.documo_api_key || 'your_api_key_here'}`);
      lines.push(`DOCUMO_SANDBOX=${config.documo_use_sandbox ? 'true' : 'false'}`);
    }

    lines.push('');
    lines.push('# Required for admin console');
    lines.push('ENABLE_LOCAL_ADMIN=true');

    const content = lines.join('\n');
    setEnvContent(content);
    return content;
  };

  const applyAndReload = async () => {
    setValidating(true);
    setEnvContent('');
    setValidationResults(null);
    try {
      const payload: any = {
        backend: config.backend,
        require_api_key: config.require_api_key,
        enforce_public_https: config.enforce_public_https,
        pdf_token_ttl_minutes: config.pdf_token_ttl_minutes,
      };
      if (config.backend === 'phaxio') {
        payload.phaxio_api_key = config.phaxio_api_key;
        payload.phaxio_api_secret = config.phaxio_api_secret;
        if (config.public_api_url) payload.public_api_url = config.public_api_url;
      } else if (config.backend === 'sinch') {
        payload.sinch_project_id = config.sinch_project_id;
        payload.sinch_api_key = config.sinch_api_key;
        payload.sinch_api_secret = config.sinch_api_secret;
      } else if (config.backend === 'documo') {
        payload.documo_api_key = config.documo_api_key;
        payload.documo_use_sandbox = config.documo_use_sandbox;
      } else if (config.backend === 'sip') {
        payload.ami_host = config.ami_host;
        payload.ami_port = config.ami_port;
        payload.ami_username = config.ami_username;
        payload.ami_password = config.ami_password;
        payload.fax_station_id = config.fax_station_id;
      }
      await client.updateSettings(payload);
      await client.reloadSettings();
      const results = await client.validateSettings(config);
      setValidationResults(results);
      setSnack('Configuration applied and reloaded');
      // Optionally return to Dashboard
      try { sessionStorage.setItem('fb_admin_applied','1'); } catch {}
      setTimeout(() => onDone?.(), 500);
    } catch (e) {
      setValidationResults({ error: e instanceof Error ? e.message : 'Apply failed' });
    } finally {
      setValidating(false);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  const downloadEnv = (text: string) => {
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'faxbot.env';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const renderStepContent = (step: number) => {
    switch (step) {
      case 0:
        return (
          <Box>
            <Typography variant="h6" gutterBottom>
              Choose Your Fax Backend
            </Typography>
            
            <FormControl fullWidth sx={{ mt: 2 }}>
              <InputLabel>Backend Type</InputLabel>
              <Select
                value={config.backend}
                onChange={(e) => handleConfigChange('backend', e.target.value)}
                label="Backend Type"
              >
                <MenuItem value="phaxio">Phaxio (Cloud - Recommended)</MenuItem>
                <MenuItem value="sinch">Sinch Fax API v3 (Cloud)</MenuItem>
                <MenuItem value="documo">Documo (mFax)</MenuItem>
                <MenuItem value="sip">SIP/Asterisk (Self-hosted)</MenuItem>
              </Select>
            </FormControl>
            
            {config.backend === 'phaxio' && (
              <Alert severity="success" sx={{ mt: 2 }}>
                Best for healthcare: 5-minute setup, automatic HIPAA compliance with BAA
              </Alert>
            )}
            
            {config.backend === 'sip' && (
              <Alert severity="warning" sx={{ mt: 2 }}>
                Requires technical expertise: T.38 support, port forwarding, NAT configuration
              </Alert>
            )}
          </Box>
        );

      case 1:
        return (
          <Box>
            <Typography variant="h6" gutterBottom>
              Configure {config.backend.toUpperCase()} Credentials
            </Typography>
            <Box sx={{ display: 'flex', gap: 1, mb: 2 }}>
              <Button variant="outlined" onClick={handleValidate} disabled={validating}>
                {validating ? <CircularProgress size={18} /> : 'Validate Provider'}
              </Button>
              {validationResults && !validationResults.error && (
                <Chip color={(validationResults.checks && Object.values(validationResults.checks).every(Boolean)) ? 'success' : 'warning'} label={(validationResults.checks && Object.values(validationResults.checks).every(Boolean)) ? 'All checks passed' : 'Review checks'} />
              )}
            </Box>
            
            {config.backend === 'phaxio' && (
              <Grid container spacing={{ xs: 2, md: 3 }}>
                <Grid item xs={12}>
                  <SecretInput
                    label="API Key"
                    value={config.phaxio_api_key || ''}
                    onChange={(value) => handleConfigChange('phaxio_api_key', value)}
                    fullWidth
                    size="small"
                  />
                </Grid>
                <Grid item xs={12}>
                  <SecretInput
                    label="API Secret"
                    value={config.phaxio_api_secret || ''}
                    onChange={(value) => handleConfigChange('phaxio_api_secret', value)}
                    fullWidth
                    size="small"
                  />
                </Grid>
                <Grid item xs={12}>
                  <TextField
                    label="Public API URL"
                    value={config.public_api_url || ''}
                    onChange={(e) => handleConfigChange('public_api_url', e.target.value)}
                    fullWidth
                    size="small"
                    placeholder="https://your-domain.com"
                    helperText="Must be HTTPS and publicly accessible for Phaxio to fetch PDFs"
                  />
                </Grid>
                <Grid item xs={12}>
                  <Alert severity="info">
                    Callback URL will be: {config.public_api_url || 'https://your-domain.com'}/phaxio-callback
                  </Alert>
                </Grid>
              </Grid>
            )}

            {config.backend === 'sinch' && (
              <Grid container spacing={2}>
                <Grid item xs={12}>
                  <TextField
                    label="Project ID"
                    value={config.sinch_project_id || ''}
                    onChange={(e) => handleConfigChange('sinch_project_id', e.target.value)}
                    fullWidth
                  />
                </Grid>
                <Grid item xs={12}>
                  <SecretInput
                    label="API Key"
                    value={config.sinch_api_key || ''}
                    onChange={(value) => handleConfigChange('sinch_api_key', value)}
                    fullWidth
                  />
                </Grid>
                <Grid item xs={12}>
                  <SecretInput
                    label="API Secret"
                    value={config.sinch_api_secret || ''}
                    onChange={(value) => handleConfigChange('sinch_api_secret', value)}
                    fullWidth
                  />
                </Grid>
              </Grid>
            )}

            {config.backend === 'sip' && (
              <Grid container spacing={2}>
                <Grid item xs={12}>
                  <Alert severity="error" sx={{ mb: 2 }}>
                    AMI must NEVER be exposed to the internet. Keep port 5038 internal only.
                  </Alert>
                </Grid>
                <Grid item xs={6}>
                  <TextField
                    label="AMI Host"
                    value={config.ami_host || ''}
                    onChange={(e) => handleConfigChange('ami_host', e.target.value)}
                    fullWidth
                    placeholder="asterisk"
                  />
                </Grid>
                <Grid item xs={6}>
                  <TextField
                    label="AMI Port"
                    type="number"
                    value={config.ami_port || 5038}
                    onChange={(e) => handleConfigChange('ami_port', parseInt(e.target.value))}
                    fullWidth
                  />
                </Grid>
                <Grid item xs={6}>
                  <TextField
                    label="AMI Username"
                    value={config.ami_username || ''}
                    onChange={(e) => handleConfigChange('ami_username', e.target.value)}
                    fullWidth
                    placeholder="api"
                  />
                </Grid>
                <Grid item xs={6}>
                  <SecretInput
                    label="AMI Password"
                    value={config.ami_password || ''}
                    onChange={(value) => handleConfigChange('ami_password', value)}
                    fullWidth
                    error={config.ami_password === 'changeme'}
                    helperText={config.ami_password === 'changeme' ? 'Must change default password!' : ''}
                  />
                </Grid>
                <Grid item xs={12}>
                  <TextField
                    label="Station ID / DID"
                    value={config.fax_station_id || ''}
                    onChange={(e) => handleConfigChange('fax_station_id', e.target.value)}
                    fullWidth
                    placeholder="+15551234567"
                    helperText="Your fax number in E.164 format"
                  />
                </Grid>
              </Grid>
            )}

            {/* Provider Connect */}
            {(config.backend === 'phaxio' || config.backend === 'sinch') && (
              <Box sx={{ mt: 3 }}>
                <Typography variant="subtitle1" gutterBottom>Connect {config.backend.toUpperCase()} Inbound</Typography>
                <Button variant="outlined" onClick={loadCallbacks} sx={{ mr: 1 }}>Show Callback URL</Button>
                {callbacks && callbacks.callbacks && callbacks.callbacks[0] && (
                  <Paper sx={{ p: 2, mt: 2 }}>
                    <Typography variant="body2" sx={{ mb: 1 }}>Callback URL:</Typography>
                    <Box component="pre" sx={{ p: 1, bgcolor: 'background.default', borderRadius: 1, overflow: 'auto' }}>{callbacks.callbacks[0].url}</Box>
                <Box sx={{ mt: 1, display: 'flex', gap: 1 }}>
                  <Button variant="outlined" onClick={() => navigator.clipboard.writeText(callbacks.callbacks[0].url)}>Copy</Button>
                  <Button variant="outlined" onClick={async () => { try { await client.simulateInbound({ backend: config.backend }); setSnack('Simulated inbound received'); } catch(e:any){ setSnack(e?.message||'Simulation failed'); } }}>Simulate Inbound</Button>
                </Box>
                <Box sx={{ mt: 2, display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                  <Button variant="contained" onClick={startVerifyInbound} disabled={verifyingInbound}>
                    {verifyingInbound ? 'Waiting for inbound…' : 'Start Verify Inbound'}
                  </Button>
                  {verifyFound && (
                    <Typography variant="body2" color="success.main">Verified: inbound event received ({String(verifyFound.backend || 'unknown')})</Typography>
                  )}
                  {!verifyFound && verifyingInbound && (
                    <Typography variant="body2" color="text.secondary">Waiting for callback…</Typography>
                  )}
                </Box>
                    <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
                      Paste this URL into your {config.backend.toUpperCase()} console for inbound fax delivery.
                      {"  •  "}
                      <a href={`${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/phaxio-setup.html`} target="_blank" rel="noreferrer">Faxbot: Phaxio Setup</a>
                      {"  •  "}
                      <a href="https://developers.sinch.com/docs/fax/api-reference/" target="_blank" rel="noreferrer">Sinch Fax API Docs</a>
                      {"  •  "}
                      <a href={`${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/`} target="_blank" rel="noreferrer">Faxbot Docs</a>
                    </Typography>
                  </Paper>
                )}
              </Box>
            )}
          </Box>
        );

      case 2:
        return (
          <Box>
            <Typography variant="h6" gutterBottom>
              Security Settings
            </Typography>
            
            <Alert severity="info" sx={{ mb: 2 }}>
              These settings are critical for HIPAA compliance
            </Alert>
            
            <Grid container spacing={{ xs: 2, md: 3 }}>
              <Grid item xs={12} sm={6}>
                <FormControl fullWidth size="small">
                  <InputLabel>Require API Key</InputLabel>
                  <Select
                    value={config.require_api_key ? 'true' : 'false'}
                    onChange={(e) => handleConfigChange('require_api_key', e.target.value === 'true')}
                    label="Require API Key"
                  >
                    <MenuItem value="true">Yes (Required for HIPAA)</MenuItem>
                    <MenuItem value="false">No (Dev only)</MenuItem>
                  </Select>
                </FormControl>
              </Grid>
              <Grid item xs={12} sm={6}>
                <FormControl fullWidth size="small">
                  <InputLabel>Enforce HTTPS</InputLabel>
                  <Select
                    value={config.enforce_public_https ? 'true' : 'false'}
                    onChange={(e) => handleConfigChange('enforce_public_https', e.target.value === 'true')}
                    label="Enforce HTTPS"
                  >
                    <MenuItem value="true">Yes (Required for PHI)</MenuItem>
                    <MenuItem value="false">No (Dev only)</MenuItem>
                  </Select>
                </FormControl>
              </Grid>
              <Grid item xs={12} sm={6}>
                <FormControl fullWidth size="small">
                  <InputLabel>Enable Audit Logging</InputLabel>
                  <Select
                    value={config.audit_log_enabled ? 'true' : 'false'}
                    onChange={(e) => handleConfigChange('audit_log_enabled', e.target.value === 'true')}
                    label="Enable Audit Logging"
                  >
                    <MenuItem value="true">Yes (HIPAA requirement)</MenuItem>
                    <MenuItem value="false">No</MenuItem>
                  </Select>
                </FormControl>
              </Grid>
              <Grid item xs={12} sm={6}>
                <TextField
                  label="PDF Token TTL (minutes)"
                  type="number"
                  value={config.pdf_token_ttl_minutes || 60}
                  onChange={(e) => handleConfigChange('pdf_token_ttl_minutes', parseInt(e.target.value))}
                  fullWidth
                  size="small"
                  helperText="How long tokenized PDF URLs remain valid"
                />
              </Grid>
            </Grid>
          </Box>
        );

      case 3:
        return (
          <Box>
            <Typography variant="h6" gutterBottom>
              Apply & Export
            </Typography>
            
            <Box sx={{ mb: 2 }}>
              <Button
                variant="outlined"
                onClick={handleValidate}
                disabled={validating}
                sx={{ mr: 1 }}
              >
                {validating ? <CircularProgress size={20} /> : 'Validate Configuration'}
              </Button>
              <Button
                variant="contained"
                color="primary"
                onClick={applyAndReload}
                disabled={validating}
                sx={{ mr: 1 }}
              >
                {validating ? <CircularProgress size={20} /> : 'Apply & Reload'}
              </Button>
              <Button
                variant="contained"
                onClick={() => generateEnvContent()}
              >
                Generate .env
              </Button>
            </Box>

            {validationResults && (
              <Paper sx={{ p: 2, mb: 2 }}>
                <Typography variant="subtitle1" gutterBottom>
                  Validation Results
                </Typography>
                {validationResults.error ? (
                  <Alert severity="error">{validationResults.error}</Alert>
                ) : (
                  <Box>
                    {Object.entries(validationResults.checks || {}).map(([key, value]) => (
                      <Box key={key} display="flex" justifyContent="space-between" mb={1} alignItems="center">
                        <Box>
                          <Typography>{key.replace(/_/g,' ')}:</Typography>
                          <Typography variant="caption" color="text.secondary">
                            {config.backend === 'phaxio' && key.includes('auth') ? 'Set PHAXIO_API_KEY/PHAXIO_API_SECRET in Phaxio console.' : ''}
                            {config.backend === 'sinch' && key.includes('auth') ? 'Set SINCH project, key and secret in Sinch Fax API.' : ''}
                          </Typography>
                        </Box>
                        <Chip size="small" color={value ? 'success' : 'error'} label={value ? 'Pass' : 'Fail'} />
                      </Box>
                    ))}
                    <Typography variant="caption" color="text.secondary">
                      Help: <a href={`${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/phaxio-setup.html`} target="_blank" rel="noreferrer">Faxbot: Phaxio</a> • <a href="https://developers.sinch.com/docs/fax/api-reference/" target="_blank" rel="noreferrer">Sinch Fax API</a>
                    </Typography>
                  </Box>
                )}
              </Paper>
            )}

            {envContent && (
              <Box>
                <Box display="flex" gap={1} mb={2}>
                  <Button
                    variant="outlined"
                    onClick={() => copyToClipboard(envContent)}
                  >
                    Copy
                  </Button>
                  <Button
                    variant="outlined"
                    onClick={() => downloadEnv(envContent)}
                  >
                    Download
                  </Button>
                </Box>
                
                <Paper sx={{ p: 2, bgcolor: 'background.default' }}>
                  <pre style={{ margin: 0, fontSize: '0.875rem', overflow: 'auto' }}>
                    {envContent}
                  </pre>
                </Paper>
                
                <Alert severity="info" sx={{ mt: 2 }}>
                  Changes were applied in-process. For persistence across restarts, click Generate .env, update your environment file, and restart the API.
                </Alert>
              </Box>
            )}
          </Box>
        );

      default:
        return null;
    }
  };

  return (
    <Box>
      <Typography variant="h4" component="h1" gutterBottom>
        Setup Wizard
      </Typography>
      
      <Stepper activeStep={activeStep} sx={{ mb: 4 }}>
        {steps.map((label) => (
          <Step key={label}>
            <StepLabel>{label}</StepLabel>
          </Step>
        ))}
      </Stepper>
      
      <Card>
        <CardContent>
          {renderStepContent(activeStep)}
        </CardContent>
      </Card>
      
      <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 3 }}>
        <Button
          disabled={activeStep === 0}
          onClick={handleBack}
        >
          Back
        </Button>
        
        <Button
          variant="contained"
          onClick={handleNext}
          disabled={activeStep === steps.length - 1}
        >
          {activeStep === steps.length - 1 ? 'Finish' : 'Next'}
        </Button>
      </Box>
      {snack && (
        <Alert severity="success" sx={{ mt: 2 }} onClose={() => setSnack(null)}>
          {snack}
        </Alert>
      )}
    </Box>
  );
}

export default SetupWizard;
