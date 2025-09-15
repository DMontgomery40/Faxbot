import { useState } from 'react';
import { Box, Card, CardContent, Typography, Stepper, Step, StepLabel, Button, TextField, Alert, Link, FormControl, InputLabel, Select, MenuItem, Tooltip, Chip, useMediaQuery, useTheme } from '@mui/material';
import InfoIcon from '@mui/icons-material/Info';
import WarningIcon from '@mui/icons-material/Warning';
import SecurityIcon from '@mui/icons-material/Security';

type Backend = 'sinch' | 'documo' | 'phaxio' | 'sip' | 'test';

interface Props {
  onApply?: (env: Record<string,string>) => Promise<void> | void;
}

export default function SetupWizard({ onApply }: Props) {
  const [step, setStep] = useState(0);
  const [backend, setBackend] = useState<Backend>('sinch');
  const [env, setEnv] = useState<Record<string,string>>({});
  const [saving, setSaving] = useState(false);
  const [hipaaMode, setHipaaMode] = useState(false);
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));
  const steps = ['Choose Backend', 'Credentials', 'Review & Apply'];

  const set = (k: string, v: string) => setEnv((e) => ({ ...e, [k]: v }));

  // HIPAA Compliance Warning Component
  const HIPAAWarning = () => (
    <Alert severity="warning" sx={{ mb: 2 }} icon={<SecurityIcon />}>
      <Typography variant="subtitle2" fontWeight="bold" gutterBottom>
        HIPAA Compliance Requirements
      </Typography>
      <Typography variant="body2" paragraph>
        {backend === 'test' ? (
          <>Test mode should NEVER be used with real PHI data.</>
        ) : backend === 'sip' ? (
          <>Self-hosted requires network isolation, strong AMI passwords, and T.38 encryption when handling PHI.</>
        ) : (
          <>Cloud providers require a signed Business Associate Agreement (BAA) before handling any PHI.</>
        )}
      </Typography>
      {backend !== 'test' && backend !== 'sip' && (
        <Box>
          <Typography variant="body2" sx={{ mb: 1 }}>Required steps for HIPAA compliance:</Typography>
          <ol style={{ margin: 0, paddingLeft: 20, fontSize: '0.875rem' }}>
            <li>Sign BAA with provider</li>
            <li>Disable document storage at provider</li>
            <li>Enable two-factor authentication</li>
            <li>Use HTTPS for all webhooks</li>
            <li>Enable signature verification</li>
          </ol>
        </Box>
      )}
      <Link href="/Faxbot/security/hipaa-requirements.html" target="_blank" rel="noopener" sx={{ display: 'inline-flex', alignItems: 'center', mt: 1 }}>
        <InfoIcon sx={{ fontSize: 16, mr: 0.5 }} />
        Learn more about HIPAA requirements
      </Link>
    </Alert>
  );

  const Links = () => {
    if (backend === 'test') {
      return (
        <Alert severity="success" sx={{ mb: 2 }}>
          <Typography variant="body2" gutterBottom>
            <strong>Test/Development Mode</strong> - No actual fax transmission
          </Typography>
          <ul style={{ margin: 0, paddingLeft: 20, fontSize: '0.875rem' }}>
            <li>Simulates all API responses</li>
            <li>File processing and validation works normally</li>
            <li>Perfect for development and CI/CD pipelines</li>
            <li>NO real faxes are sent</li>
          </ul>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
            Set FAX_DISABLED=true to enable test mode
          </Typography>
        </Alert>
      );
    }
    
    if (backend === 'sinch') {
      return (
        <Alert severity="info" sx={{ mb: 2 }}>
          <Box display="flex" alignItems="center" mb={1}>
            <Chip label="No Domain Required" size="small" color="success" sx={{ mr: 1 }} />
            <Tooltip title="Direct upload means you don't need a public URL or domain to send faxes">
              <InfoIcon fontSize="small" color="action" />
            </Tooltip>
          </Box>
          <Typography variant="body2" gutterBottom>
            Sinch Fax API v3 (Direct Upload) - Modern cloud fax service
          </Typography>
          <Box sx={{ mt: 1 }}>
            <Link href="https://dashboard.sinch.com/signup" target="_blank" rel="noopener">Sign up</Link> ·{' '}
            <Link href="https://dashboard.sinch.com" target="_blank" rel="noopener">Dashboard</Link> ·{' '}
            <Link href="https://developers.sinch.com/docs/fax/overview/" target="_blank" rel="noopener">API Docs</Link> ·{' '}
            <Link href="/Faxbot/backends/sinch-setup.html" target="_blank" rel="noopener">Setup Guide</Link>
          </Box>
        </Alert>
      );
    }
    if (backend === 'documo') {
      return (
        <Alert severity="info" sx={{ mb: 2 }}>
          <Box display="flex" alignItems="center" mb={1}>
            <Chip label="No Domain Required" size="small" color="success" sx={{ mr: 1 }} />
            <Tooltip title="Direct upload API - no callbacks or public URL needed">
              <InfoIcon fontSize="small" color="action" />
            </Tooltip>
          </Box>
          <Typography variant="body2" gutterBottom>
            Documo (mFax) - Alternative cloud fax provider
          </Typography>
          <Box sx={{ mt: 1 }}>
            <Link href="https://www.mfax.io/pricing" target="_blank" rel="noopener">Sign up</Link> ·{' '}
            <Link href="https://app.documo.com" target="_blank" rel="noopener">Web App</Link> ·{' '}
            <Link href="https://docs.documo.com" target="_blank" rel="noopener">API Docs</Link> ·{' '}
            <Link href="/Faxbot/backends/documo-setup.html" target="_blank" rel="noopener">Setup Guide</Link>
          </Box>
        </Alert>
      );
    }
    if (backend === 'phaxio') {
      return (
        <Alert severity="info" sx={{ mb: 2 }}>
          <Box display="flex" alignItems="center" mb={1}>
            <Chip label="Domain/Tunnel Required" size="small" color="warning" sx={{ mr: 1 }} />
            <Tooltip title="Phaxio needs to fetch PDFs from your server via HTTPS">
              <InfoIcon fontSize="small" color="action" />
            </Tooltip>
          </Box>
          <Typography variant="body2" gutterBottom>
            Phaxio (by Sinch) - Enterprise-grade cloud fax with callbacks
          </Typography>
          <Box sx={{ mt: 1 }}>
            <Link href="https://dashboard.sinch.com/signup" target="_blank" rel="noopener">Sign up</Link> ·{' '}
            <Link href="https://www.phaxio.com/docs/" target="_blank" rel="noopener">Phaxio Docs</Link> ·{' '}
            <Link href="/Faxbot/backends/phaxio-setup.html" target="_blank" rel="noopener">Setup Guide</Link>
          </Box>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
            💡 Tip: Use scripts/setup-phaxio-tunnel.sh for quick testing without a domain
          </Typography>
        </Alert>
      );
    }
    // SIP
    return (
      <Alert severity="warning" sx={{ mb: 2 }}>
        <Box display="flex" alignItems="center" mb={1}>
          <Chip label="Advanced Setup" size="small" color="error" sx={{ mr: 1 }} />
          <Chip label="Full Control" size="small" color="primary" sx={{ mr: 1 }} />
          <Tooltip title="Requires SIP trunk provider, T.38 knowledge, and network configuration">
            <InfoIcon fontSize="small" color="action" />
          </Tooltip>
        </Box>
        <Typography variant="body2" gutterBottom>
          SIP/Asterisk - Self-hosted with complete control
        </Typography>
        <Alert severity="error" sx={{ mt: 1, mb: 1 }}>
          <strong>⚠️ Security Critical:</strong> AMI port 5038 must NEVER be exposed publicly!
        </Alert>
        <Box sx={{ mt: 1 }}>
          <Link href="/Faxbot/backends/sip-setup.html" target="_blank" rel="noopener">SIP/Asterisk Setup Guide</Link> ·{' '}
          <Link href="/Faxbot/guides/signalwire-setup.html" target="_blank" rel="noopener">SignalWire Example</Link>
        </Box>
      </Alert>
    );
  };

  const Fields = () => {
    switch (backend) {
      case 'sinch':
        return (
          <Box>
            <TextField label="SINCH_PROJECT_ID" value={env.SINCH_PROJECT_ID||''} onChange={(e)=>set('SINCH_PROJECT_ID', e.target.value)} fullWidth margin="normal" />
            <TextField label="SINCH_API_KEY" value={env.SINCH_API_KEY||''} onChange={(e)=>set('SINCH_API_KEY', e.target.value)} fullWidth margin="normal" />
            <TextField label="SINCH_API_SECRET" value={env.SINCH_API_SECRET||''} onChange={(e)=>set('SINCH_API_SECRET', e.target.value)} fullWidth margin="normal" />
          </Box>
        );
      case 'documo':
        return (
          <Box>
            <TextField label="DOCUMO_API_KEY" value={env.DOCUMO_API_KEY||''} onChange={(e)=>set('DOCUMO_API_KEY', e.target.value)} fullWidth margin="normal" />
            <FormControl fullWidth margin="normal" size="small">
              <InputLabel>DOCUMO_SANDBOX</InputLabel>
              <Select value={env.DOCUMO_SANDBOX||'false'} label="DOCUMO_SANDBOX" onChange={(e)=>set('DOCUMO_SANDBOX', String((e.target as any).value))}>
                <MenuItem value={'false'}>false</MenuItem>
                <MenuItem value={'true'}>true</MenuItem>
              </Select>
            </FormControl>
          </Box>
        );
      case 'phaxio':
        return (
          <Box>
            <TextField label="PHAXIO_API_KEY" value={env.PHAXIO_API_KEY||''} onChange={(e)=>set('PHAXIO_API_KEY', e.target.value)} fullWidth margin="normal" />
            <TextField label="PHAXIO_API_SECRET" value={env.PHAXIO_API_SECRET||''} onChange={(e)=>set('PHAXIO_API_SECRET', e.target.value)} fullWidth margin="normal" />
            <TextField label="PUBLIC_API_URL" value={env.PUBLIC_API_URL||''} onChange={(e)=>set('PUBLIC_API_URL', e.target.value)} fullWidth margin="normal" helperText="Required if provider must fetch your PDF or send callbacks" />
          </Box>
        );
      default:
        return (
          <Box>
            <TextField label="ASTERISK_AMI_HOST" value={env.ASTERISK_AMI_HOST||''} onChange={(e)=>set('ASTERISK_AMI_HOST', e.target.value)} fullWidth margin="normal" />
            <TextField label="ASTERISK_AMI_PORT" value={env.ASTERISK_AMI_PORT||'5038'} onChange={(e)=>set('ASTERISK_AMI_PORT', e.target.value)} fullWidth margin="normal" />
            <TextField label="ASTERISK_AMI_USERNAME" value={env.ASTERISK_AMI_USERNAME||''} onChange={(e)=>set('ASTERISK_AMI_USERNAME', e.target.value)} fullWidth margin="normal" />
            <TextField label="ASTERISK_AMI_PASSWORD" value={env.ASTERISK_AMI_PASSWORD||''} onChange={(e)=>set('ASTERISK_AMI_PASSWORD', e.target.value)} fullWidth margin="normal" />
          </Box>
        );
    }
  };

  const Review = () => (
    <Box>
      <Alert severity="success" sx={{ mb: 2 }}>Ready to apply settings.</Alert>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>These values will be written to the running API environment.</Typography>
      <Card variant="outlined">
        <CardContent>
          <pre style={{ margin: 0, fontSize: '0.85rem' }}>{Object.entries({ FAX_BACKEND: backend, ...env }).map(([k,v]) => `${k}=${v}`).join('\n')}</pre>
        </CardContent>
      </Card>
    </Box>
  );

  const apply = async () => {
    try {
      setSaving(true);
      if (onApply) await onApply({ FAX_BACKEND: backend, ...env });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Box>
      <Typography variant="h4" gutterBottom>Setup Wizard</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Pick a backend, follow the inline links to create credentials, paste them, and apply.</Typography>

      <Stepper activeStep={step} sx={{ mb: 3 }}>
        {steps.map((label) => (<Step key={label}><StepLabel>{label}</StepLabel></Step>))}
      </Stepper>

      <Card>
        <CardContent>
          {step === 0 && (
            <Box>
              <FormControl fullWidth margin="normal" size="small">
                <InputLabel>Backend</InputLabel>
                <Select value={backend} label="Backend" onChange={(e)=>setBackend(e.target.value as Backend)}>
                  <MenuItem value="sinch">Sinch (Direct Upload)</MenuItem>
                  <MenuItem value="documo">Documo (mFax)</MenuItem>
                  <MenuItem value="phaxio">Phaxio (Fetch/Callback)</MenuItem>
                  <MenuItem value="sip">SIP/Asterisk (Self‑hosted)</MenuItem>
                </Select>
              </FormControl>
              <Links />
            </Box>
          )}
          {step === 1 && (
            <Box>
              <Links />
              <Fields />
            </Box>
          )}
          {step === 2 && (<Review />)}

          <Box display="flex" justifyContent="space-between" sx={{ mt: 3 }}>
            <Button disabled={step===0} onClick={()=>setStep((s)=>Math.max(0,s-1))}>Back</Button>
            {step < steps.length - 1 ? (
              <Button variant="contained" onClick={()=>setStep((s)=>s+1)}>Next</Button>
            ) : (
              <Button variant="contained" onClick={apply} disabled={saving}>{saving ? 'Applying…' : 'Apply'}</Button>
            )}
          </Box>
        </CardContent>
      </Card>
    </Box>
  );
}

