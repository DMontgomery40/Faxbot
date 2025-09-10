import { useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Button,
  Alert,
  Paper,
  CircularProgress,
  Grid,
  Chip,
  List,
  ListItem,
  ListItemText,
  ListItemIcon,
} from '@mui/material';
import {
  Refresh as RefreshIcon,
  Download as DownloadIcon,
  ContentCopy as ContentCopyIcon,
  Security as SecurityIcon,
  Cloud as CloudIcon,
  CheckCircle as CheckCircleIcon,
  Warning as WarningIcon,
  Error as ErrorIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { Settings as SettingsType } from '../api/types';

interface SettingsProps {
  client: AdminAPIClient;
}

function Settings({ client }: SettingsProps) {
  const [settings, setSettings] = useState<SettingsType | null>(null);
  const [envContent, setEnvContent] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchSettings = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.getSettings();
      setSettings(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch settings');
    } finally {
      setLoading(false);
    }
  };

  const exportEnv = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.exportSettings();
      setEnvContent(data.env_content);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to export settings');
    } finally {
      setLoading(false);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  const downloadText = (filename: string, text: string) => {
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const getStatusIcon = (configured: boolean) => {
    return configured ? (
      <CheckCircleIcon color="success" />
    ) : (
      <ErrorIcon color="error" />
    );
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          Settings
        </Typography>
        <Box>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchSettings}
            disabled={loading}
            sx={{ mr: 1 }}
          >
            Load Settings
          </Button>
          <Button
            variant="contained"
            onClick={exportEnv}
            disabled={loading}
          >
            Export .env
          </Button>
        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      <Alert severity="info" sx={{ mb: 3 }}>
        <Typography variant="body2">
          <strong>v1 Note:</strong> Settings are read-only in this version. 
          To modify settings, update your .env file and restart the API.
        </Typography>
      </Alert>

      {loading && !settings ? (
        <Box display="flex" justifyContent="center" py={4}>
          <CircularProgress />
        </Box>
      ) : settings ? (
        <Grid container spacing={3}>
          {/* Backend Configuration */}
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Box display="flex" alignItems="center" mb={2}>
                  <CloudIcon sx={{ mr: 1 }} />
                  <Typography variant="h6">Backend Configuration</Typography>
                </Box>
                <List dense>
                  <ListItem>
                    <ListItemIcon>
                      {getStatusIcon(!settings.backend.disabled)}
                    </ListItemIcon>
                    <ListItemText
                      primary="Backend Type"
                      secondary={settings.backend.type.toUpperCase()}
                    />
                    <Chip
                      label={settings.backend.disabled ? 'Disabled' : 'Active'}
                      color={settings.backend.disabled ? 'error' : 'success'}
                      size="small"
                      variant="outlined"
                    />
                  </ListItem>
                </List>
              </CardContent>
            </Card>
          </Grid>

          {/* Security Settings */}
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Box display="flex" alignItems="center" mb={2}>
                  <SecurityIcon sx={{ mr: 1 }} />
                  <Typography variant="h6">Security</Typography>
                </Box>
                <List dense>
                  <ListItem>
                    <ListItemIcon>
                      {settings.security.require_api_key ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="API Key Required"
                      secondary={settings.security.require_api_key ? 'Yes' : 'No'}
                    />
                  </ListItem>
                  <ListItem>
                    <ListItemIcon>
                      {settings.security.enforce_https ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="HTTPS Enforced"
                      secondary={settings.security.enforce_https ? 'Yes' : 'No'}
                    />
                  </ListItem>
                  <ListItem>
                    <ListItemIcon>
                      {settings.security.audit_enabled ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="Audit Logging"
                      secondary={settings.security.audit_enabled ? 'Enabled' : 'Disabled'}
                    />
                  </ListItem>
                </List>
              </CardContent>
            </Card>
          </Grid>

          {/* Backend-Specific Configuration */}
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  {settings.backend.type.toUpperCase()} Configuration
                </Typography>
                
                {settings.backend.type === 'phaxio' && (
                  <List dense>
                    <ListItem>
                      <ListItemIcon>{getStatusIcon(!!settings.phaxio.api_key)}</ListItemIcon>
                      <ListItemText
                        primary="API Key"
                        secondary={settings.phaxio.api_key || 'Not configured'}
                      />
                    </ListItem>
                    <ListItem>
                      <ListItemIcon>{getStatusIcon(!!settings.phaxio.api_secret)}</ListItemIcon>
                      <ListItemText
                        primary="API Secret"
                        secondary={settings.phaxio.api_secret || 'Not configured'}
                      />
                    </ListItem>
                    <ListItem>
                      <ListItemIcon>{getStatusIcon(!!settings.phaxio.callback_url)}</ListItemIcon>
                      <ListItemText
                        primary="Callback URL"
                        secondary={settings.phaxio.callback_url || 'Not configured'}
                      />
                    </ListItem>
                  </List>
                )}

                {settings.backend.type === 'sip' && (
                  <List dense>
                    <ListItem>
                      <ListItemIcon>{getStatusIcon(!!settings.sip.ami_host)}</ListItemIcon>
                      <ListItemText
                        primary="AMI Host"
                        secondary={settings.sip.ami_host || 'Not configured'}
                      />
                    </ListItem>
                    <ListItem>
                      <ListItemIcon>
                        {settings.sip.ami_password_is_default ? <WarningIcon color="warning" /> : <CheckCircleIcon color="success" />}
                      </ListItemIcon>
                      <ListItemText
                        primary="AMI Password"
                        secondary={settings.sip.ami_password_is_default ? 'Using default (insecure)' : 'Custom password set'}
                      />
                    </ListItem>
                    <ListItem>
                      <ListItemIcon>{getStatusIcon(!!settings.sip.station_id)}</ListItemIcon>
                      <ListItemText
                        primary="Station ID"
                        secondary={settings.sip.station_id || 'Not configured'}
                      />
                    </ListItem>
                  </List>
                )}
              </CardContent>
            </Card>
          </Grid>
        </Grid>
      ) : (
        <Typography variant="body2" color="text.secondary">
          Click "Load Settings" to view current configuration
        </Typography>
      )}

      {envContent && (
        <Card sx={{ mt: 3 }}>
          <CardContent>
            <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
              <Typography variant="h6">
                Environment Configuration
              </Typography>
              <Box>
                <Button
                  variant="outlined"
                  startIcon={<ContentCopyIcon />}
                  onClick={() => copyToClipboard(envContent)}
                  sx={{ mr: 1 }}
                >
                  Copy
                </Button>
                <Button
                  variant="outlined"
                  startIcon={<DownloadIcon />}
                  onClick={() => downloadText('faxbot.env', envContent)}
                >
                  Download
                </Button>
              </Box>
            </Box>
            
            <Paper sx={{ p: 2, bgcolor: 'background.default' }}>
              <pre style={{ 
                margin: 0, 
                fontSize: '0.875rem', 
                overflow: 'auto',
                whiteSpace: 'pre-wrap'
              }}>
                {envContent}
              </pre>
            </Paper>
            
            <Alert severity="warning" sx={{ mt: 2 }}>
              After updating your .env file, restart the API with: <code>docker compose restart api</code>
            </Alert>
          </CardContent>
        </Card>
      )}
    </Box>
  );
}

export default Settings;
