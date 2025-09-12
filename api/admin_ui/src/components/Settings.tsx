import { useEffect, useState } from 'react';
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
  const [snack, setSnack] = useState<string | null>(null);
  const [form, setForm] = useState<any>({});
  const [restartHint, setRestartHint] = useState<boolean>(false);
  const [allowRestart, setAllowRestart] = useState<boolean>(false);
  const handleForm = (field: string, value: any) => setForm((prev: any) => ({ ...prev, [field]: value }));

  const fetchSettings = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.getSettings();
      setSettings(data);
      setForm({
        backend: data.backend?.type,
        require_api_key: data.security?.require_api_key,
        enforce_public_https: data.security?.enforce_https,
        public_api_url: data.security?.public_api_url,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch settings');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    (async () => {
      await fetchSettings();
      try {
        const cfg = await client.getConfig();
        setAllowRestart(!!cfg?.allow_restart);
      } catch {}
    })();
  }, []);

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
        <Typography variant="body2">Apply changes live, then export .env for persistence across restarts.</Typography>
      </Alert>

      {loading && !settings ? (
        <Box display="flex" justifyContent="center" py={4}>
          <CircularProgress />
        </Box>
      ) : settings ? (
        <Box>
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
                    <select
                      value={form.backend || settings.backend.type}
                      onChange={(e) => handleForm('backend', e.target.value)}
                      style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                    >
                      <option value="phaxio">phaxio</option>
                      <option value="sinch">sinch</option>
                      <option value="sip">sip</option>
                    </select>
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
                    <select
                      value={(form.require_api_key ?? settings.security.require_api_key) ? 'true' : 'false'}
                      onChange={(e) => handleForm('require_api_key', e.target.value === 'true')}
                      style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                    >
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  </ListItem>
                  <ListItem>
                    <ListItemIcon>
                      {settings.security.enforce_https ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="HTTPS Enforced"
                      secondary={settings.security.enforce_https ? 'Yes' : 'No'}
                    />
                    <select
                      value={(form.enforce_public_https ?? settings.security.enforce_https) ? 'true' : 'false'}
                      onChange={(e) => handleForm('enforce_public_https', e.target.value === 'true')}
                      style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                    >
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  </ListItem>
                  <ListItem>
                    <ListItemIcon>
                      {settings.security.audit_enabled ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="Audit Logging"
                      secondary={settings.security.audit_enabled ? 'Enabled' : 'Disabled'}
                    />
                    <select
                      value={(form.audit_log_enabled ?? settings.security.audit_enabled) ? 'true' : 'false'}
                      onChange={(e) => handleForm('audit_log_enabled', e.target.value === 'true')}
                      style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                    >
                      <option value="true">Enabled</option>
                      <option value="false">Disabled</option>
                    </select>
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
                      <input
                        placeholder="Update PHAXIO_API_KEY"
                        onChange={(e) => handleForm('phaxio_api_key', e.target.value)}
                        style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                      />
                    </ListItem>
                    <ListItem>
                      <ListItemIcon>{getStatusIcon(!!settings.phaxio.api_secret)}</ListItemIcon>
                      <ListItemText
                        primary="API Secret"
                        secondary={settings.phaxio.api_secret || 'Not configured'}
                      />
                      <input
                        placeholder="Update PHAXIO_API_SECRET"
                        onChange={(e) => handleForm('phaxio_api_secret', e.target.value)}
                        style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                      />
                    </ListItem>
                    <ListItem>
                      <ListItemIcon>{getStatusIcon(!!settings.phaxio.callback_url)}</ListItemIcon>
                      <ListItemText
                        primary="Callback URL"
                        secondary={settings.phaxio.callback_url || 'Not configured'}
                      />
                      <input
                        placeholder="PUBLIC_API_URL"
                        defaultValue={form.public_api_url || ''}
                        onChange={(e) => handleForm('public_api_url', e.target.value)}
                        style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6, minWidth: '220px' }}
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
                      <input
                        placeholder="ASTERISK_AMI_HOST"
                        onChange={(e) => handleForm('ami_host', e.target.value)}
                        style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
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
                      <input
                        placeholder="Update ASTERISK_AMI_PASSWORD"
                        onChange={(e) => handleForm('ami_password', e.target.value)}
                        style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                      />
                    </ListItem>
                    <ListItem>
                      <ListItemIcon>{getStatusIcon(!!settings.sip.station_id)}</ListItemIcon>
                      <ListItemText
                        primary="Station ID"
                        secondary={settings.sip.station_id || 'Not configured'}
                      />
                      <input
                        placeholder="FAX_LOCAL_STATION_ID"
                        onChange={(e) => handleForm('fax_station_id', e.target.value)}
                        style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                      />
                    </ListItem>
                  </List>
                )}
              </CardContent>
            </Card>
          </Grid>

          {/* Storage Configuration */}
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Storage Configuration
                </Typography>
                <List dense>
                  <ListItem>
                    <ListItemText primary="Storage Backend" secondary={(form.storage_backend || settings.storage?.backend || 'local').toUpperCase()} />
                    <select
                      value={form.storage_backend || settings.storage?.backend || 'local'}
                      onChange={(e) => handleForm('storage_backend', e.target.value)}
                      style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                    >
                      <option value="local">local</option>
                      <option value="s3">s3</option>
                    </select>
                  </ListItem>
                  {(form.storage_backend === 's3' || settings.storage?.backend === 's3') && (
                    <>
                      <ListItem>
                        <ListItemText primary="S3 Bucket" secondary={settings.storage?.s3_bucket || ''} />
                        <input placeholder="S3_BUCKET" onChange={(e)=>handleForm('s3_bucket', e.target.value)} style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }} />
                      </ListItem>
                      <ListItem>
                        <ListItemText primary="S3 Region" secondary={settings.storage?.s3_region || ''} />
                        <input placeholder="S3_REGION" onChange={(e)=>handleForm('s3_region', e.target.value)} style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }} />
                      </ListItem>
                      <ListItem>
                        <ListItemText primary="S3 Prefix" secondary={settings.storage?.s3_prefix || ''} />
                        <input placeholder="S3_PREFIX" onChange={(e)=>handleForm('s3_prefix', e.target.value)} style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }} />
                      </ListItem>
                      <ListItem>
                        <ListItemText primary="S3 Endpoint URL" secondary={settings.storage?.s3_endpoint_url || ''} />
                        <input placeholder="S3_ENDPOINT_URL" onChange={(e)=>handleForm('s3_endpoint_url', e.target.value)} style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6, minWidth: '260px' }} />
                      </ListItem>
                      <ListItem>
                        <ListItemText primary="S3 KMS Key ID" secondary={settings.storage?.s3_kms_enabled ? 'Configured' : 'Not set'} />
                        <input placeholder="S3_KMS_KEY_ID" onChange={(e)=>handleForm('s3_kms_key_id', e.target.value)} style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6, minWidth: '260px' }} />
                      </ListItem>
                      <ListItem>
                        <Button variant="outlined" onClick={async ()=>{ try { setLoading(true); const diag = await (client as any).runDiagnostics?.(); if (diag?.checks?.storage?.type === 's3') { const st = diag.checks.storage; const ok = st.accessible===true || st.bucket_set; setSnack(ok ? 'S3 validation passed' : ('S3 validation incomplete' + (st.error? (': '+st.error):''))); } else { setSnack('Diagnostics did not include S3 checks. Enable ENABLE_S3_DIAGNOSTICS=true on server for full validation.'); } } catch(e:any){ setError(e?.message||'S3 validation failed'); } finally { setLoading(false);} }}>
                          Validate S3
                        </Button>
                        <Typography variant="caption" sx={{ ml: 2 }}>Full validation requires ENABLE_S3_DIAGNOSTICS=true on server and proper AWS credentials via env/role.</Typography>
                      </ListItem>
                    </>
                  )}
                </List>
              </CardContent>
            </Card>
          </Grid>
        </Grid>
        <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
          <Button variant="contained" onClick={async () => { try { setLoading(true); setError(null); setRestartHint(false); const p:any={}; if (form.backend) p.backend=form.backend; if (form.require_api_key!==undefined) p.require_api_key=!!form.require_api_key; if (form.enforce_public_https!==undefined) p.enforce_public_https=!!form.enforce_public_https; if (form.public_api_url) p.public_api_url=String(form.public_api_url); if (form.backend==='phaxio'){ if (form.phaxio_api_key) p.phaxio_api_key=form.phaxio_api_key; if (form.phaxio_api_secret) p.phaxio_api_secret=form.phaxio_api_secret; } if (form.backend==='sinch'){ if (form.sinch_project_id) p.sinch_project_id=form.sinch_project_id; if (form.sinch_api_key) p.sinch_api_key=form.sinch_api_key; if (form.sinch_api_secret) p.sinch_api_secret=form.sinch_api_secret; } if (form.backend==='sip'){ if (form.ami_host) p.ami_host=form.ami_host; if (form.ami_port) p.ami_port=Number(form.ami_port); if (form.ami_username) p.ami_username=form.ami_username; if (form.ami_password) p.ami_password=form.ami_password; if (form.fax_station_id) p.fax_station_id=form.fax_station_id; } if (form.storage_backend) p.storage_backend=form.storage_backend; if (form.s3_bucket) p.s3_bucket=form.s3_bucket; if (form.s3_region) p.s3_region=form.s3_region; if (form.s3_prefix) p.s3_prefix=form.s3_prefix; if (form.s3_endpoint_url) p.s3_endpoint_url=form.s3_endpoint_url; if (form.s3_kms_key_id) p.s3_kms_key_id=form.s3_kms_key_id; const res = await client.updateSettings(p); await client.reloadSettings(); await fetchSettings(); setSnack('Settings applied and reloaded'); if (res && res._meta && res._meta.restart_recommended) setRestartHint(true); } catch(e:any){ setError(e?.message||'Failed to apply settings'); } finally { setLoading(false);} }} disabled={loading}>
            Apply & Reload
          </Button>
          <Button variant="outlined" startIcon={<RefreshIcon />} onClick={fetchSettings} disabled={loading}>
            Refresh
          </Button>
        </Box>
        {restartHint && (
          <Alert severity="warning" sx={{ mt: 2 }}>
            Changes may require an API restart (backend or storage changed). {allowRestart ? 'You can restart below.' : 'Please restart the API process.'}
          </Alert>
        )}
        {allowRestart && (
          <Box sx={{ mt: 1 }}>
            <Button variant="outlined" onClick={async () => { try { await client.restart(); } catch (e) { /* ignore */ } }}>
              Restart API
            </Button>
          </Box>
        )}
        </Box>
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
      {snack && (
        <Alert severity="success" sx={{ mt: 2 }} onClose={() => setSnack(null)}>
          {snack}
        </Alert>
      )}
    </Box>
  );
}

export default Settings;
