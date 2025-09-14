import { useState } from 'react';
import { Box, Card, CardContent, Typography, Stepper, Step, StepLabel, Button, TextField, Alert, Link, FormControl, InputLabel, Select, MenuItem } from '@mui/material';

type Backend = 'sinch' | 'documo' | 'phaxio' | 'sip';

interface Props {
  onApply?: (env: Record<string,string>) => Promise<void> | void;
}

export default function SetupWizard({ onApply }: Props) {
  const [step, setStep] = useState(0);
  const [backend, setBackend] = useState<Backend>('sinch');
  const [env, setEnv] = useState<Record<string,string>>({});
  const [saving, setSaving] = useState(false);
  const steps = ['Choose Backend', 'Credentials', 'Review & Apply'];

  const set = (k: string, v: string) => setEnv((e) => ({ ...e, [k]: v }));

  const Links = () => {
    if (backend === 'sinch') {
      return (
        <Alert severity="info" sx={{ mb: 2 }}>
          <div>Sinch (Direct Upload) works without a domain.</div>
          <div>
            <Link href="https://dashboard.sinch.com/signup" target="_blank" rel="noopener">Sign up</Link> ·{' '}
            <Link href="https://dashboard.sinch.com" target="_blank" rel="noopener">Open Dashboard (Project & API Keys)</Link> ·{' '}
            <Link href="https://developers.sinch.com/docs/fax/overview/" target="_blank" rel="noopener">Fax Docs</Link>
          </div>
        </Alert>
      );
    }
    if (backend === 'documo') {
      return (
        <Alert severity="info" sx={{ mb: 2 }}>
          <div>Documo (mFax) direct upload — no domain required.</div>
          <div>
            <Link href="https://www.mfax.io/pricing" target="_blank" rel="noopener">Sign up</Link> ·{' '}
            <Link href="https://app.documo.com" target="_blank" rel="noopener">Open Web App (Settings → API)</Link> ·{' '}
            <Link href="https://docs.documo.com" target="_blank" rel="noopener">API Docs</Link>
          </div>
        </Alert>
      );
    }
    if (backend === 'phaxio') {
      return (
        <Alert severity="info" sx={{ mb: 2 }}>
          <div>Phaxio by Sinch — may require a public HTTPS URL for callbacks/fetch.</div>
          <div>
            <Link href="https://dashboard.sinch.com/signup" target="_blank" rel="noopener">Sign up</Link> ·{' '}
            <Link href="https://www.phaxio.com/docs/" target="_blank" rel="noopener">Phaxio Docs</Link>
          </div>
        </Alert>
      );
    }
    // SIP
    return (
      <Alert severity="info" sx={{ mb: 2 }}>
        <div>SIP/Asterisk (self‑hosted). Requires SIP trunk and T.38 knowledge.</div>
        <div>
          <Link href="/Faxbot/backends/sip-setup.html" target="_blank" rel="noopener">SIP/Asterisk Setup</Link>
        </div>
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

