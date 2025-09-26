import { useState } from 'react';
import {
  Box,
  Typography,
  Card,
  CardContent,
  Button,
  Alert,
  Chip,
  Stack,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Stepper,
  Step,
  StepLabel,
  Grid,
  Paper,
  Fade,
  useTheme,
  useMediaQuery,
} from '@mui/material';
import {
  Router as SipIcon,
  CheckCircle as CheckIcon,
  Settings as SettingsIcon,
  Security as SecurityIcon,
  Webhook as WebhookIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import { useTraits } from '../hooks/useTraits';
import { getProviderIcon } from '../utils/providerIcons';
import {
  ResponsiveTextField,
  ResponsiveCheckbox,
  ResponsiveFormSection,
} from './common/ResponsiveFormFields';

interface ProviderSetupWizardProps {
  client: AdminAPIClient;
  open: boolean;
  onClose: () => void;
  onComplete: () => void;
}

interface ProviderConfig {
  provider: string;
  // Phaxio
  phaxio_api_key?: string;
  phaxio_api_secret?: string;
  phaxio_callback_url?: string;
  phaxio_verify_signature?: boolean;
  // Sinch
  sinch_project_id?: string;
  sinch_api_key?: string;
  sinch_api_secret?: string;
  sinch_base_url?: string;
  // Documo
  documo_api_key?: string;
  documo_use_sandbox?: boolean;
  // SignalWire
  signalwire_space_url?: string;
  signalwire_project_id?: string;
  signalwire_api_token?: string;
  signalwire_fax_from?: string;
  signalwire_callback_url?: string;
  // SIP/Asterisk
  ami_host?: string;
  ami_port?: number;
  ami_username?: string;
  ami_password?: string;
  fax_station_id?: string;
  // Security
  public_api_url?: string;
  enforce_https?: boolean;
}

const PROVIDER_OPTIONS = [
  { value: 'phaxio', label: 'Phaxio (Cloud)', category: 'cloud' },
  { value: 'sinch', label: 'Sinch Fax API', category: 'cloud' },
  { value: 'documo', label: 'Documo', category: 'cloud' },
  { value: 'signalwire', label: 'SignalWire', category: 'cloud' },
  { value: 'sip', label: 'SIP/Asterisk', category: 'self-hosted' },
  { value: 'freeswitch', label: 'FreeSWITCH', category: 'self-hosted' },
  { value: 'disabled', label: 'Test Mode', category: 'test' },
];

const STEPS = ['Choose Provider', 'Configure Credentials', 'Review & Save'];

export default function ProviderSetupWizard({
  client,
  open,
  onClose,
  onComplete,
}: ProviderSetupWizardProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const { registry } = useTraits();

  const [activeStep, setActiveStep] = useState(0);
  const [config, setConfig] = useState<ProviderConfig>({ provider: '' });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [validationResult, setValidationResult] = useState<any>(null);

  const handleConfigChange = (field: string, value: any) => {
    setConfig(prev => ({ ...prev, [field]: value }));
  };

  const handleNext = () => {
    if (activeStep === STEPS.length - 1) {
      handleSave();
    } else {
      setActiveStep(prev => prev + 1);
    }
  };

  const handleBack = () => {
    setActiveStep(prev => prev - 1);
  };

  const handleValidate = async () => {
    try {
      setLoading(true);
      setError(null);
      
      // Convert config to settings format
      const settings: any = { backend: config.provider };
      // Traits-driven mapping
      const t = registry?.[config.provider]?.traits || {};
      const methods = (t?.auth?.methods || []) as string[];
      const requiresAmi = Boolean(t?.requires_ami);
      const basicOnly = Array.isArray(methods) && methods.includes('basic') && !methods.includes('oauth2');
      const hasOAuth = Array.isArray(methods) && methods.includes('oauth2');

      if (basicOnly) {
        settings.phaxio_api_key = config.phaxio_api_key;
        settings.phaxio_api_secret = config.phaxio_api_secret;
        settings.phaxio_callback_url = config.phaxio_callback_url;
        settings.phaxio_verify_signature = config.phaxio_verify_signature;
      }
      if (hasOAuth) {
        settings.sinch_project_id = config.sinch_project_id;
        settings.sinch_api_key = config.sinch_api_key;
        settings.sinch_api_secret = config.sinch_api_secret;
        settings.sinch_base_url = config.sinch_base_url;
      }
      if (requiresAmi) {
        settings.ami_host = config.ami_host;
        settings.ami_port = config.ami_port;
        settings.ami_username = config.ami_username;
        settings.ami_password = config.ami_password;
        settings.fax_station_id = config.fax_station_id;
      }

      if (config.public_api_url) {
        settings.public_api_url = config.public_api_url;
      }
      if (config.enforce_https !== undefined) {
        settings.enforce_public_https = config.enforce_https;
      }

      const result = await client.validateSettings(settings);
      setValidationResult(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Validation failed');
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    try {
      setLoading(true);
      setError(null);

      // Convert config to settings format and save
      const settings: any = { backend: config.provider };
      const t = registry?.[config.provider]?.traits || {};
      const methods = (t?.auth?.methods || []) as string[];
      const requiresAmi = Boolean(t?.requires_ami);
      const basicOnly = Array.isArray(methods) && methods.includes('basic') && !methods.includes('oauth2');
      const hasOAuth = Array.isArray(methods) && methods.includes('oauth2');

      if (basicOnly) {
        settings.phaxio_api_key = config.phaxio_api_key;
        settings.phaxio_api_secret = config.phaxio_api_secret;
        if (config.public_api_url) settings.public_api_url = config.public_api_url;
      }
      if (hasOAuth) {
        settings.sinch_project_id = config.sinch_project_id;
        settings.sinch_api_key = config.sinch_api_key;
        settings.sinch_api_secret = config.sinch_api_secret;
        if (config.sinch_base_url) settings.sinch_base_url = config.sinch_base_url;
      }
      if (requiresAmi) {
        settings.ami_host = config.ami_host;
        settings.ami_port = config.ami_port;
        settings.ami_username = config.ami_username;
        settings.ami_password = config.ami_password;
        settings.fax_station_id = config.fax_station_id;
      }

      await client.updateSettings(settings);
      onComplete();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save configuration');
    } finally {
      setLoading(false);
    }
  };

  const getProviderCapabilities = (providerId: string) => {
    if (!registry || !registry[providerId]?.traits) return [];
    
    const traits = registry[providerId].traits;
    const capabilities: string[] = [];
    
    if (traits.supports_inbound) capabilities.push('Inbound Fax');
    if (traits.requires_ami) capabilities.push('AMI Required');
    if (traits.requires_tiff) capabilities.push('TIFF Conversion');
    if (traits.needs_storage) capabilities.push('Storage Required');
    if (traits.inbound_verification && traits.inbound_verification !== 'none') {
      capabilities.push(`${traits.inbound_verification.toUpperCase()} Verification`);
    }
    
    return capabilities;
  };

  const renderProviderSelection = () => (
    <Box>
      <Typography variant="h6" gutterBottom>
        Choose Your Fax Provider
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Select the provider that best fits your needs
      </Typography>

      <Grid container spacing={2}>
        {['cloud', 'self-hosted', 'test'].map(category => (
          <Grid item xs={12} key={category}>
            <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1, textTransform: 'capitalize' }}>
              {category === 'self-hosted' ? 'Self-Hosted' : category} Providers
            </Typography>
            <Grid container spacing={2}>
              {PROVIDER_OPTIONS.filter(p => p.category === category).map(provider => {
                const capabilities = getProviderCapabilities(provider.value);
                const isSelected = config.provider === provider.value;
                
                return (
                  <Grid item xs={12} sm={6} md={4} key={provider.value}>
                    <Card
                      sx={{
                        cursor: 'pointer',
                        border: '2px solid',
                        borderColor: isSelected ? 'primary.main' : 'divider',
                        backgroundColor: isSelected ? 'primary.50' : 'background.paper',
                        '&:hover': {
                          borderColor: 'primary.main',
                          backgroundColor: 'primary.50',
                        },
                        transition: 'all 0.2s',
                      }}
                      onClick={() => handleConfigChange('provider', provider.value)}
                    >
                      <CardContent>
                        <Box sx={{ display: 'flex', alignItems: 'center', mb: 1 }}>
                          <Box sx={{ mr: 1 }}>
                            {getProviderIcon(provider.value, 'large')}
                          </Box>
                          <Typography variant="subtitle1" fontWeight={600}>
                            {provider.label}
                          </Typography>
                          {isSelected && <CheckIcon color="primary" sx={{ ml: 'auto' }} />}
                        </Box>
                        
                        {capabilities.length > 0 && (
                          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mt: 1 }}>
                            {capabilities.map(cap => (
                              <Chip
                                key={cap}
                                label={cap}
                                size="small"
                                variant="outlined"
                                sx={{ borderRadius: 1 }}
                              />
                            ))}
                          </Box>
                        )}
                      </CardContent>
                    </Card>
                  </Grid>
                );
              })}
            </Grid>
          </Grid>
        ))}
      </Grid>
    </Box>
  );

  const renderCredentialsForm = () => {
    const provider = PROVIDER_OPTIONS.find(p => p.value === config.provider);
    if (!provider) return null;

    return (
      <Box>
        <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
          <Box sx={{ mr: 1 }}>
            {getProviderIcon(config.provider, 'large')}
          </Box>
          <Typography variant="h6">
            Configure {provider.label}
          </Typography>
        </Box>

        <Stack spacing={3}>
          {(() => { const t = registry?.[config.provider]?.traits || {}; const m = (t?.auth?.methods||[]) as string[]; return Array.isArray(m) && m.includes('basic') && !m.includes('oauth2'); })() && (
            <ResponsiveFormSection
              title="Phaxio API Credentials"
              subtitle="Get these from your Phaxio console"
              icon={<SecurityIcon />}
            >
              <Stack spacing={2}>
                <ResponsiveTextField
                  label="API Key"
                  value={config.phaxio_api_key || ''}
                  onChange={(value) => handleConfigChange('phaxio_api_key', value)}
                  placeholder="your_api_key_from_console"
                  required
                />
                <ResponsiveTextField
                  label="API Secret"
                  value={config.phaxio_api_secret || ''}
                  onChange={(value) => handleConfigChange('phaxio_api_secret', value)}
                  placeholder="your_api_secret_from_console"
                  type="password"
                  required
                />
                <ResponsiveTextField
                  label="Callback URL"
                  value={config.phaxio_callback_url || ''}
                  onChange={(value) => handleConfigChange('phaxio_callback_url', value)}
                  placeholder="https://yourdomain.com/phaxio-callback"
                  helperText="URL where Phaxio will send status updates"
                />
                <ResponsiveCheckbox
                  label="Verify Webhook Signatures"
                  checked={config.phaxio_verify_signature || false}
                  onChange={(checked) => handleConfigChange('phaxio_verify_signature', checked)}
                  helperText="Recommended for production (HIPAA compliance)"
                />
              </Stack>
            </ResponsiveFormSection>
          )}

          {(() => { const t = registry?.[config.provider]?.traits || {}; const m = (t?.auth?.methods||[]) as string[]; return Array.isArray(m) && m.includes('oauth2'); })() && (
            <ResponsiveFormSection
              title="Sinch API Credentials"
              subtitle="Get these from your Sinch dashboard"
              icon={<SecurityIcon />}
            >
              <Stack spacing={2}>
                <ResponsiveTextField
                  label="Project ID"
                  value={config.sinch_project_id || ''}
                  onChange={(value) => handleConfigChange('sinch_project_id', value)}
                  placeholder="your_project_id"
                  required
                />
                <ResponsiveTextField
                  label="API Key"
                  value={config.sinch_api_key || ''}
                  onChange={(value) => handleConfigChange('sinch_api_key', value)}
                  placeholder="your_api_key"
                  required
                />
                <ResponsiveTextField
                  label="API Secret"
                  value={config.sinch_api_secret || ''}
                  onChange={(value) => handleConfigChange('sinch_api_secret', value)}
                  placeholder="your_api_secret"
                  type="password"
                  required
                />
                <ResponsiveTextField
                  label="Base URL (Optional)"
                  value={config.sinch_base_url || ''}
                  onChange={(value) => handleConfigChange('sinch_base_url', value)}
                  placeholder="https://us.fax.api.sinch.com/v3"
                  helperText="Leave blank for default, or specify regional endpoint"
                />
              </Stack>
            </ResponsiveFormSection>
          )}

          {Boolean(registry?.[config.provider]?.traits?.requires_ami) && (
            <ResponsiveFormSection
              title="Asterisk AMI Configuration"
              subtitle="Configure connection to your Asterisk server"
              icon={<SipIcon />}
            >
              <Stack spacing={2}>
                <ResponsiveTextField
                  label="AMI Host"
                  value={config.ami_host || ''}
                  onChange={(value) => handleConfigChange('ami_host', value)}
                  placeholder="asterisk"
                  required
                />
                <ResponsiveTextField
                  label="AMI Port"
                  value={config.ami_port?.toString() || ''}
                  onChange={(value) => handleConfigChange('ami_port', parseInt(value) || 5038)}
                  placeholder="5038"
                  type="number"
                />
                <ResponsiveTextField
                  label="AMI Username"
                  value={config.ami_username || ''}
                  onChange={(value) => handleConfigChange('ami_username', value)}
                  placeholder="api"
                  required
                />
                <ResponsiveTextField
                  label="AMI Password"
                  value={config.ami_password || ''}
                  onChange={(value) => handleConfigChange('ami_password', value)}
                  placeholder="secure_password_not_changeme"
                  type="password"
                  required
                />
                <ResponsiveTextField
                  label="Station ID"
                  value={config.fax_station_id || ''}
                  onChange={(value) => handleConfigChange('fax_station_id', value)}
                  placeholder="My Faxbot"
                  helperText="Identifier shown on outbound faxes"
                />
              </Stack>
            </ResponsiveFormSection>
          )}

          {/* Common Security Settings */}
          <ResponsiveFormSection
            title="Security Settings"
            subtitle="Configure security and webhook settings"
            icon={<WebhookIcon />}
          >
            <Stack spacing={2}>
              <ResponsiveTextField
                label="Public API URL"
                value={config.public_api_url || ''}
                onChange={(value) => handleConfigChange('public_api_url', value)}
                placeholder="https://yourdomain.com"
                helperText="Public URL for webhook callbacks"
              />
              <ResponsiveCheckbox
                label="Enforce HTTPS"
                checked={config.enforce_https || false}
                onChange={(checked) => handleConfigChange('enforce_https', checked)}
                helperText="Required for HIPAA compliance"
              />
            </Stack>
          </ResponsiveFormSection>
        </Stack>
      </Box>
    );
  };

  const renderReview = () => {
    const provider = PROVIDER_OPTIONS.find(p => p.value === config.provider);
    const capabilities = getProviderCapabilities(config.provider);

    return (
      <Box>
        <Typography variant="h6" gutterBottom>
          Review Configuration
        </Typography>
        
        <Stack spacing={3}>
          <Paper elevation={0} sx={{ p: 2, border: '1px solid', borderColor: 'divider' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
          <Box sx={{ mr: 1 }}>
            {getProviderIcon(config.provider, 'large')}
          </Box>
              <Typography variant="h6">
                {provider?.label}
              </Typography>
            </Box>
            
            {capabilities.length > 0 && (
              <Box>
                <Typography variant="subtitle2" sx={{ mb: 1 }}>
                  Capabilities:
                </Typography>
                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                  {capabilities.map(cap => (
                    <Chip
                      key={cap}
                      label={cap}
                      size="small"
                      color="primary"
                      variant="outlined"
                      sx={{ borderRadius: 1 }}
                    />
                  ))}
                </Box>
              </Box>
            )}
          </Paper>

          {validationResult && (
            <Alert 
              severity={validationResult.test_fax?.sent ? 'success' : 'warning'}
              sx={{ borderRadius: 2 }}
            >
              {validationResult.test_fax?.sent 
                ? 'Configuration validated successfully!'
                : 'Configuration saved, but test transmission failed. Check your credentials and try again.'
              }
            </Alert>
          )}

          <Box sx={{ display: 'flex', gap: 1 }}>
            <Button
              variant="outlined"
              onClick={handleValidate}
              disabled={loading}
              startIcon={<CheckIcon />}
            >
              Test Configuration
            </Button>
          </Box>
        </Stack>
      </Box>
    );
  };

  const renderStepContent = () => {
    switch (activeStep) {
      case 0:
        return renderProviderSelection();
      case 1:
        return renderCredentialsForm();
      case 2:
        return renderReview();
      default:
        return null;
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="md"
      fullWidth
      fullScreen={isMobile}
    >
      <DialogTitle>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <SettingsIcon />
          Provider Setup Wizard
        </Box>
      </DialogTitle>
      
      <DialogContent>
        <Box sx={{ width: '100%', mb: 3 }}>
          <Stepper activeStep={activeStep} alternativeLabel={!isMobile}>
            {STEPS.map((label) => (
              <Step key={label}>
                <StepLabel>{label}</StepLabel>
              </Step>
            ))}
          </Stepper>
        </Box>

        {error && (
          <Alert severity="error" sx={{ mb: 3, borderRadius: 2 }}>
            {error}
          </Alert>
        )}

        <Fade in key={activeStep}>
          <Box>{renderStepContent()}</Box>
        </Fade>
      </DialogContent>

      <DialogActions sx={{ px: 3, pb: 3 }}>
        <Button onClick={onClose} disabled={loading}>
          Cancel
        </Button>
        <Button
          onClick={handleBack}
          disabled={activeStep === 0 || loading}
        >
          Back
        </Button>
        <Button
          variant="contained"
          onClick={handleNext}
          disabled={!config.provider || loading}
        >
          {activeStep === STEPS.length - 1 ? 'Save Configuration' : 'Next'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
