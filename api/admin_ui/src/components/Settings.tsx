import { useEffect, useState } from 'react';
import { useMediaQuery } from '@mui/material';
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
  Switch,
  FormGroup,
  FormControlLabel,
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
  const [persistedEnabled, setPersistedEnabled] = useState<boolean>(false);
  const [lastGeneratedSecret, setLastGeneratedSecret] = useState<string>('');
  const handleForm = (field: string, value: any) => setForm((prev: any) => ({ ...prev, [field]: value }));
  const isSmall = useMediaQuery('(max-width:900px)');
  const ctlStyle: React.CSSProperties = { background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6, width: isSmall ? '100%' : 'auto', maxWidth: isSmall ? '100%' : undefined };
  const numCtlStyle: React.CSSProperties = { ...ctlStyle, width: isSmall ? '100%' : '120px' } as React.CSSProperties;

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
        feature_v3_plugins: data.features?.v3_plugins,
        fax_disabled: data.backend?.disabled,
        inbound_enabled: data.inbound?.enabled,
        feature_plugin_install: data.features?.plugin_install,
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
        setPersistedEnabled(!!cfg?.persisted_settings_enabled);
        setForm((prev: any) => ({ ...prev, enable_persisted_settings: !!cfg?.persisted_settings_enabled }));
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
          <Button variant="contained" onClick={exportEnv} disabled={loading} sx={{ mr: 1 }}>
            Export .env
          </Button>
          <Button
            variant="outlined"
            onClick={async () => {
              try {
                setLoading(true); setError(null);
                const res = await client.persistSettings();
                setSnack(`Saved to ${res.path}`);
              } catch (e: any) {
                setError(e?.message || 'Failed to save on server');
              } finally { setLoading(false); }
            }}
            disabled={loading}
          >
            Save .env to server
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
                <Box id="settings-backend" display="flex" alignItems="center" mb={2}>
                  <CloudIcon sx={{ mr: 1 }} />
                  <Typography variant="h6">Backend Configuration</Typography>
                </Box>
                <List dense>
                  <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                    <ListItemIcon>
                      {getStatusIcon(!settings.backend.disabled)}
                    </ListItemIcon>
                    <ListItemText
                      primary="Backend Type"
                      secondary={settings.backend.type.toUpperCase()}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Choose your transport backend. Changing providers may require a restart and
                      provider-specific configuration. For SIP/Asterisk, ensure private networking and T.38 support.
                    </Typography>
                    <select
                      value={form.backend || settings.backend.type}
                      onChange={(e) => handleForm('backend', e.target.value)}
                      style={ctlStyle}
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
                <Box id="settings-security" display="flex" alignItems="center" mb={2}>
                  <SecurityIcon sx={{ mr: 1 }} />
                  <Typography variant="h6">Security</Typography>
                </Box>
                <List dense>
                  <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                    <ListItemIcon>
                      {settings.security.require_api_key ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="API Key Required"
                      secondary={settings.security.require_api_key ? 'Yes' : 'No'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Enable in production; required for HIPAA. Mint DB-backed keys in the Keys tab and pass them as X-API-Key.
                    </Typography>
                    <select
                      value={(form.require_api_key ?? settings.security.require_api_key) ? 'true' : 'false'}
                      onChange={(e) => handleForm('require_api_key', e.target.value === 'true')}
                      style={ctlStyle}
                    >
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  </ListItem>
                  <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                    <ListItemIcon>
                      {settings.security.enforce_https ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="HTTPS Enforced"
                      secondary={settings.security.enforce_https ? 'Yes' : 'No'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Required for PHI. PUBLIC_API_URL must be HTTPS for cloud providers to fetch PDFs securely.
                    </Typography>
                    <select
                      value={(form.enforce_public_https ?? settings.security.enforce_https) ? 'true' : 'false'}
                      onChange={(e) => handleForm('enforce_public_https', e.target.value === 'true')}
                      style={ctlStyle}
                    >
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  </ListItem>
                  <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                    <ListItemIcon>
                      {settings.security.audit_enabled ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="Audit Logging"
                      secondary={settings.security.audit_enabled ? 'Enabled' : 'Disabled'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Enable structured logs for admin actions and fax lifecycle. Set AUDIT_LOG_FILE to persist; view in Logs tab.
                    </Typography>
                    <select
                      value={(form.audit_log_enabled ?? settings.security.audit_enabled) ? 'true' : 'false'}
                      onChange={(e) => handleForm('audit_log_enabled', e.target.value === 'true')}
                      style={ctlStyle}
                    >
                      <option value="true">Enabled</option>
                      <option value="false">Disabled</option>
                    </select>
                  </ListItem>
                  <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                    <ListItemIcon>
                      {persistedEnabled ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="Load persisted .env at startup"
                      secondary={persistedEnabled ? 'Enabled' : 'Disabled'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Loads /faxdata/faxbot.env at boot. Use “Save .env to server” after applying changes to keep them across restarts.
                    </Typography>
                    <select
                      value={(form.enable_persisted_settings ?? persistedEnabled) ? 'true' : 'false'}
                      onChange={(e) => handleForm('enable_persisted_settings', e.target.value === 'true')}
                      style={ctlStyle}
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
                      <Typography id="settings-phaxio" variant="subtitle1">Phaxio</Typography>
                    </ListItem>
                    <ListItem>
                      <Typography variant="caption" color="text.secondary">
                        Help: 
                        <a href="https://dmontgomery40.github.io/Faxbot/providers/phaxio/" target="_blank" rel="noreferrer">Faxbot: Phaxio</a>
                        {"  •  "}
                        <a href="https://developers.sinch.com/docs/fax/api-reference/" target="_blank" rel="noreferrer">Sinch Fax API</a>
                      </Typography>
                    </ListItem>
                  <ListItem>
                    <ListItemIcon>{getStatusIcon(!!settings.phaxio.api_key)}</ListItemIcon>
                    <ListItemText
                      primary="API Key"
                      secondary={settings.phaxio.api_key || 'Not configured'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Get from Phaxio console. Use a service account and keep this secret safe.
                    </Typography>
                    <input
                      placeholder="Update PHAXIO_API_KEY"
                      onChange={(e) => handleForm('phaxio_api_key', e.target.value)}
                      style={ctlStyle}
                    />
                  </ListItem>
                  <ListItem>
                    <ListItemIcon>{getStatusIcon(!!settings.phaxio.api_secret)}</ListItemIcon>
                    <ListItemText
                      primary="API Secret"
                      secondary={settings.phaxio.api_secret || 'Not configured'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Get from Phaxio console. Required alongside API key for provider API calls.
                    </Typography>
                    <input
                      placeholder="Update PHAXIO_API_SECRET"
                      onChange={(e) => handleForm('phaxio_api_secret', e.target.value)}
                      style={ctlStyle}
                    />
                  </ListItem>
                  <ListItem>
                    <ListItemIcon>{getStatusIcon(!!settings.phaxio.callback_url)}</ListItemIcon>
                    <ListItemText
                      primary="Callback URL"
                      secondary={settings.phaxio.callback_url || 'Not configured'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Phaxio status webhooks hit /phaxio-callback; PUBLIC_API_URL must be HTTPS. Enable HMAC verification for security.
                    </Typography>
                    <input
                      placeholder="PUBLIC_API_URL"
                      defaultValue={form.public_api_url || ''}
                      onChange={(e) => handleForm('public_api_url', e.target.value)}
                      style={ctlStyle}
                    />
                  </ListItem>
                  </List>
                )}

                {settings.backend.type === 'sip' && (
                  <List dense>
                    <ListItem>
                      <Typography id="settings-sip" variant="subtitle1">SIP / Asterisk</Typography>
                    </ListItem>
                  <ListItem>
                    <ListItemIcon>{getStatusIcon(!!settings.sip.ami_host)}</ListItemIcon>
                    <ListItemText
                      primary="AMI Host"
                      secondary={settings.sip.ami_host || 'Not configured'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Asterisk service hostname on your private network (e.g., docker compose service name "asterisk").
                    </Typography>
                    <input
                      placeholder="ASTERISK_AMI_HOST"
                      onChange={(e) => handleForm('ami_host', e.target.value)}
                      style={ctlStyle}
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
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Must not be the default. Update in both Faxbot and Asterisk manager.conf; never expose 5038 publicly.
                    </Typography>
                    <input
                      placeholder="Update ASTERISK_AMI_PASSWORD"
                      onChange={(e) => handleForm('ami_password', e.target.value)}
                      style={ctlStyle}
                    />
                  </ListItem>
                  <ListItem>
                    <ListItemIcon>{getStatusIcon(!!settings.sip.station_id)}</ListItemIcon>
                    <ListItemText
                      primary="Station ID"
                      secondary={settings.sip.station_id || 'Not configured'}
                    />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Your fax header/DID in E.164 format (e.g., +15551234567).
                    </Typography>
                    <input
                      placeholder="FAX_LOCAL_STATION_ID"
                      onChange={(e) => handleForm('fax_station_id', e.target.value)}
                      style={ctlStyle}
                    />
                  </ListItem>
                  </List>
                )}
              </CardContent>
            </Card>
          </Grid>

          {/* Feature Flags */}
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Feature Flags
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                  Enable or disable v3 features. Changes require restart to take effect.
                </Typography>
                
                <FormGroup>
                  <FormControlLabel
                    control={
                      <Switch
                        checked={form.feature_v3_plugins ?? settings?.features?.v3_plugins ?? false}
                        onChange={(e) => handleForm('feature_v3_plugins', e.target.checked)}
                      />
                    }
                    label="Enable v3 Plugin System"
                  />
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 4, mb: 2 }}>
                    Activates the new modular plugin architecture for fax providers
                  </Typography>
                  
                  <FormControlLabel
                    control={
                      <Switch
                        checked={form.fax_disabled ?? settings?.backend?.disabled ?? false}
                        onChange={(e) => handleForm('fax_disabled', e.target.checked)}
                      />
                    }
                    label="Test Mode (No Real Faxes)"
                  />
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 4, mb: 2 }}>
                    Simulates fax operations without actually sending - useful for development
                  </Typography>
                  
                  <FormControlLabel
                    control={
                      <Switch
                        checked={form.inbound_enabled ?? settings?.inbound?.enabled ?? false}
                        onChange={(e) => handleForm('inbound_enabled', e.target.checked)}
                      />
                    }
                    label="Enable Inbound Fax Receiving"
                  />
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 4, mb: 2 }}>
                    Allow receiving faxes (requires additional configuration based on backend)
                  </Typography>

                  <FormControlLabel
                    control={
                      <Switch
                        checked={form.feature_plugin_install ?? settings?.features?.plugin_install ?? false}
                        onChange={(e) => handleForm('feature_plugin_install', e.target.checked)}
                        disabled
                      />
                    }
                    label="Allow Remote Plugin Installation (Advanced)"
                  />
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 4, mb: 2 }}>
                    Disabled by default for security. Enable only in trusted environments.
                  </Typography>
                </FormGroup>

                {restartHint && (
                  <Alert severity="info" sx={{ mt: 2 }}>
                    Feature flag changes require a restart to take effect
                  </Alert>
                )}
              </CardContent>
            </Card>
          </Grid>

          {/* Inbound Receiving */}
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography id="settings-inbound" variant="h6" gutterBottom>
                  Inbound Receiving
                </Typography>
                <List dense>
                  <ListItem>
                    <ListItemIcon>
                      {settings.inbound?.enabled ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                    </ListItemIcon>
                    <ListItemText
                      primary="Enable Inbound"
                      secondary={settings.inbound?.enabled ? 'Enabled' : 'Disabled'}
                    />
                    <select
                      value={(form.inbound_enabled ?? settings.inbound?.enabled) ? 'true' : 'false'}
                      onChange={(e) => handleForm('inbound_enabled', e.target.value === 'true')}
                      style={ctlStyle}
                    >
                      <option value="true">Enabled</option>
                      <option value="false">Disabled</option>
                    </select>
                  </ListItem>
                  <ListItem>
                    <ListItemText
                      primary="Retention Days"
                      secondary={String(settings.inbound?.retention_days ?? 30)}
                    />
                    <input
                      type="number"
                      placeholder={String(settings.inbound?.retention_days ?? 30)}
                      onChange={(e) => handleForm('inbound_retention_days', parseInt(e.target.value))}
                      style={numCtlStyle}
                    />
                  </ListItem>
                  <ListItem>
                    <ListItemText
                      primary="Token TTL (minutes)"
                      secondary={String(settings.inbound?.token_ttl_minutes ?? 60)}
                    />
                    <input
                      type="number"
                      placeholder={String(settings.inbound?.token_ttl_minutes ?? 60)}
                      onChange={(e) => handleForm('inbound_token_ttl_minutes', parseInt(e.target.value))}
                      style={numCtlStyle}
                    />
                  </ListItem>

                  {settings.backend.type === 'sip' && (
                    <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                      <ListItemText
                        primary="Asterisk Inbound Secret"
                        secondary={
                          <Typography variant="caption" color="text.secondary">
                            Shared secret used by your Asterisk dialplan to POST inbound fax metadata to Faxbot.
                            Keep this private and only use it on the private network.
                            {' '}<a href="https://dmontgomery40.github.io/Faxbot/asterisk/inbound/" target="_blank" rel="noreferrer">Asterisk inbound guide</a>
                          </Typography>
                        }
                      />
                      <input
                        placeholder="ASTERISK_INBOUND_SECRET"
                        onChange={(e) => handleForm('asterisk_inbound_secret', e.target.value)}
                        style={{ background: 'transparent', color: 'inherit', borderColor: '#444', padding: '6px', borderRadius: 6 }}
                      />
                      <Button size="small" sx={{ ml: 1 }} onClick={async ()=>{
                        try {
                          const bytes = new Uint8Array(32);
                          const cryptoObj: any = (typeof window !== 'undefined') ? (window as any).crypto : undefined;
                          if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
                            cryptoObj.getRandomValues(bytes);
                          } else {
                            for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
                          }
                          const b64 = btoa(String.fromCharCode(...Array.from(bytes))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
                          setLastGeneratedSecret(b64);
                          await client.updateSettings({ asterisk_inbound_secret: b64 });
                          await client.reloadSettings();
                          await fetchSettings();
                          setSnack('Generated new inbound secret (displayed once below)');
                        } catch(e:any){ setError(e?.message||'Failed to generate secret'); }
                      }}>Generate</Button>
                      <Button size="small" onClick={async ()=>{
                        const toCopy = (form.asterisk_inbound_secret || lastGeneratedSecret || '').trim();
                        if (!toCopy) return;
                        try { await navigator.clipboard.writeText(toCopy); setSnack('Copied'); } catch {}
                      }} disabled={!form.asterisk_inbound_secret && !lastGeneratedSecret}>Copy</Button>
                      {lastGeneratedSecret && (
                        <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                          New secret (copy now): <code>{lastGeneratedSecret}</code>
                        </Typography>
                      )}
                    </ListItem>
                  )}

                  {settings.backend.type === 'phaxio' && (
                    <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                      <ListItemText
                        primary="Verify Phaxio Inbound Signature"
                        secondary={settings.inbound?.phaxio?.verify_signature ? 'Enabled' : 'Disabled'}
                      />
                      <select
                        value={(form.phaxio_inbound_verify_signature ?? settings.inbound?.phaxio?.verify_signature) ? 'true' : 'false'}
                        onChange={(e) => handleForm('phaxio_inbound_verify_signature', e.target.value === 'true')}
                        style={ctlStyle}
                      >
                        <option value="true">Enabled</option>
                        <option value="false">Disabled</option>
                      </select>
                    </ListItem>
                  )}

                  {settings.backend.type === 'sinch' && (
                    <>
                      <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                        <ListItemText
                          primary="Verify Sinch Inbound Signature"
                          secondary={settings.inbound?.sinch?.verify_signature ? 'Enabled' : 'Disabled'}
                        />
                        <select
                          value={(form.sinch_inbound_verify_signature ?? settings.inbound?.sinch?.verify_signature) ? 'true' : 'false'}
                          onChange={(e) => handleForm('sinch_inbound_verify_signature', e.target.value === 'true')}
                          style={ctlStyle}
                        >
                          <option value="true">Enabled</option>
                          <option value="false">Disabled</option>
                        </select>
                      </ListItem>
                      <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                        <ListItemText
                          primary="Sinch Inbound Basic Auth"
                          secondary={settings.inbound?.sinch?.basic_auth_configured ? 'Configured' : 'Not configured'}
                        />
                        <input
                          placeholder="SINCH_INBOUND_BASIC_USER"
                          onChange={(e) => handleForm('sinch_inbound_basic_user', e.target.value)}
                          style={{ ...ctlStyle, marginRight: isSmall ? 0 : 8 }}
                        />
                        <input
                          placeholder="SINCH_INBOUND_BASIC_PASS"
                          onChange={(e) => handleForm('sinch_inbound_basic_pass', e.target.value)}
                          style={ctlStyle}
                        />
                        <Typography variant="caption" color="text.secondary" sx={{ ml: 2 }}>
                          Faxbot-enforced optional auth. Use if your provider supports setting Basic credentials on callbacks.
                        </Typography>
                      </ListItem>
                      <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                        <ListItemText
                          primary="Sinch Inbound HMAC Secret"
                          secondary={settings.inbound?.sinch?.hmac_configured ? 'Configured' : 'Not configured'}
                        />
                        <input
                          placeholder="SINCH_INBOUND_HMAC_SECRET"
                          onChange={(e) => handleForm('sinch_inbound_hmac_secret', e.target.value)}
                          style={ctlStyle}
                        />
                        <Typography variant="caption" color="text.secondary" sx={{ ml: 2 }}>
                          Faxbot-enforced optional HMAC validation. Configure the same shared secret in your provider if supported.
                          {"  •  "}
                          <a href="https://developers.sinch.com/docs/fax/api-reference/" target="_blank" rel="noreferrer">Sinch Fax API Docs</a>
                        </Typography>
                      </ListItem>
                    </>
                  )}
                </List>
              </CardContent>
            </Card>
          </Grid>

          {/* Storage Configuration */}
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography id="settings-storage" variant="h6" gutterBottom>
                  Storage Configuration
                </Typography>
                <List dense>
                  <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                    <ListItemText primary="Storage Backend" secondary={(form.storage_backend || settings.storage?.backend || 'local').toUpperCase()} />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Local for development only. Use S3 with KMS for PHI in production.
                    </Typography>
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
                      <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                        <ListItemText primary="S3 Bucket" secondary={settings.storage?.s3_bucket || ''} />
                        <input placeholder="S3_BUCKET" onChange={(e)=>handleForm('s3_bucket', e.target.value)} style={ctlStyle} />
                      </ListItem>
                      <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                        <ListItemText primary="S3 Region" secondary={settings.storage?.s3_region || ''} />
                        <input placeholder="S3_REGION" onChange={(e)=>handleForm('s3_region', e.target.value)} style={ctlStyle} />
                      </ListItem>
                      <ListItem sx={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 1 }}>
                        <ListItemText primary="S3 Prefix" secondary={settings.storage?.s3_prefix || ''} />
                        <input placeholder="S3_PREFIX" onChange={(e)=>handleForm('s3_prefix', e.target.value)} style={ctlStyle} />
                      </ListItem>
                      <ListItem>
                        <ListItemText primary="S3 Endpoint URL" secondary={settings.storage?.s3_endpoint_url || ''} />
                        <input placeholder="S3_ENDPOINT_URL" onChange={(e)=>handleForm('s3_endpoint_url', e.target.value)} style={ctlStyle} />
                      </ListItem>
                      <ListItem>
                        <ListItemText primary="S3 KMS Key ID" secondary={settings.storage?.s3_kms_enabled ? 'Configured' : 'Not set'} />
                        <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                          Enable server-side encryption with KMS by specifying a CMK (recommended for PHI).
                        </Typography>
                        <input placeholder="S3_KMS_KEY_ID" onChange={(e)=>handleForm('s3_kms_key_id', e.target.value)} style={ctlStyle} />
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

          {/* Advanced Settings */}
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography id="settings-advanced" variant="h6" gutterBottom>
                  Advanced Settings
                </Typography>
                <List dense>
                  <ListItem>
                    <ListItemText primary="Database URL" secondary={settings.database?.url || 'sqlite:///./faxbot.db'} />
                    <Button variant="outlined" onClick={async ()=>{ try{ setLoading(true); setError(null); await client.updateSettings({ database_url: 'sqlite:////faxdata/faxbot.db' }); await client.reloadSettings(); await fetchSettings(); setSnack('Switched DB to /faxdata/faxbot.db'); }catch(e:any){ setError(e?.message||'Failed to switch DB'); } finally{ setLoading(false);} }}>Use persistent DB</Button>
                    <Typography variant="caption" color="text.secondary" sx={{ ml: 2 }}>
                      SQLite in /faxdata persists across rebuilds. For production scale, use Postgres.
                    </Typography>
                  </ListItem>
                  <ListItem>
                    <Button variant="outlined" onClick={async ()=>{ try{ setLoading(true); const res = await (client as any).fetch?.('/admin/db-status'); const data = await res.json(); setEnvContent(JSON.stringify(data, null, 2)); setSnack('DB status loaded'); } catch(e:any){ setError(e?.message||'Failed to load DB status'); } finally{ setLoading(false);} }}>Check DB Status</Button>
                    <Typography variant="caption" color="text.secondary" sx={{ ml: 2 }}>Shows current driver, connection, counts and SQLite file info.</Typography>
                  </ListItem>
                  <ListItem>
                    <ListItemText primary="Max Upload Size (MB)" secondary={String(settings.limits?.max_file_size_mb || 10)} />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Default 10 MB aligns with provider limits. Increase only if your environment and provider allow it.
                    </Typography>
                    <input type="number" placeholder={String(settings.limits?.max_file_size_mb || 10)} onChange={(e)=>handleForm('max_file_size_mb', parseInt(e.target.value))} style={numCtlStyle} />
                  </ListItem>
                  <ListItem>
                    <ListItemText primary="Global Rate Limit (RPM)" secondary={String(settings.limits?.rate_limit_rpm || 0)} />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Per-key requests per minute. Set to mitigate abuse; 0 disables global rate limiting.
                    </Typography>
                    <input type="number" placeholder={String(settings.limits?.rate_limit_rpm || 0)} onChange={(e)=>handleForm('max_requests_per_minute', parseInt(e.target.value))} style={numCtlStyle} />
                  </ListItem>
                  <ListItem>
                    <ListItemText primary="Inbound List RPM" secondary={String(settings.limits?.inbound_list_rpm ?? 30)} />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Rate limit for listing inbound faxes (per key). Keep conservative for HIPAA workloads.
                    </Typography>
                    <input type="number" placeholder={String(settings.limits?.inbound_list_rpm ?? 30)} onChange={(e)=>handleForm('inbound_list_rpm', parseInt(e.target.value))} style={numCtlStyle} />
                  </ListItem>
                  <ListItem>
                    <ListItemText primary="Inbound Get RPM" secondary={String(settings.limits?.inbound_get_rpm ?? 60)} />
                    <Typography variant="caption" color="text.secondary" sx={{ mr: 2 }}>
                      Rate limit for fetching inbound fax metadata/PDF (per key).
                    </Typography>
                    <input type="number" placeholder={String(settings.limits?.inbound_get_rpm ?? 60)} onChange={(e)=>handleForm('inbound_get_rpm', parseInt(e.target.value))} style={numCtlStyle} />
                  </ListItem>
                </List>
                <Alert severity="info">
                  For HIPAA environments, set reasonable RPM limits and keep upload size within policy.
                </Alert>
              </CardContent>
            </Card>
          </Grid>
        </Grid>
        <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
          <Button variant="contained" onClick={async () => { try { setLoading(true); setError(null); setRestartHint(false); const p:any={}; if (form.backend) p.backend=form.backend; if (form.require_api_key!==undefined) p.require_api_key=!!form.require_api_key; if (form.enforce_public_https!==undefined) p.enforce_public_https=!!form.enforce_public_https; if (form.public_api_url) p.public_api_url=String(form.public_api_url); if (form.enable_persisted_settings!==undefined) p.enable_persisted_settings=!!form.enable_persisted_settings; if (form.feature_v3_plugins!==undefined) p.feature_v3_plugins=!!form.feature_v3_plugins; if (form.feature_plugin_install!==undefined) p.feature_plugin_install=!!form.feature_plugin_install; if (form.backend==='phaxio'){ if (form.phaxio_api_key) p.phaxio_api_key=form.phaxio_api_key; if (form.phaxio_api_secret) p.phaxio_api_secret=form.phaxio_api_secret; } if (form.backend==='sinch'){ if (form.sinch_project_id) p.sinch_project_id=form.sinch_project_id; if (form.sinch_api_key) p.sinch_api_key=form.sinch_api_key; if (form.sinch_api_secret) p.sinch_api_secret=form.sinch_api_secret; } if (form.backend==='sip'){ if (form.ami_host) p.ami_host=form.ami_host; if (form.ami_port) p.ami_port=Number(form.ami_port); if (form.ami_username) p.ami_username=form.ami_username; if (form.ami_password) p.ami_password=form.ami_password; if (form.fax_station_id) p.fax_station_id=form.fax_station_id; } if (form.inbound_enabled!==undefined) p.inbound_enabled=!!form.inbound_enabled; if (form.inbound_retention_days!==undefined) p.inbound_retention_days=Number(form.inbound_retention_days); if (form.inbound_token_ttl_minutes!==undefined) p.inbound_token_ttl_minutes=Number(form.inbound_token_ttl_minutes); if (form.asterisk_inbound_secret) p.asterisk_inbound_secret=form.asterisk_inbound_secret; if (form.phaxio_inbound_verify_signature!==undefined) p.phaxio_inbound_verify_signature=!!form.phaxio_inbound_verify_signature; if (form.sinch_inbound_verify_signature!==undefined) p.sinch_inbound_verify_signature=!!form.sinch_inbound_verify_signature; if (form.sinch_inbound_basic_user) p.sinch_inbound_basic_user=form.sinch_inbound_basic_user; if (form.sinch_inbound_basic_pass) p.sinch_inbound_basic_pass=form.sinch_inbound_basic_pass; if (form.sinch_inbound_hmac_secret) p.sinch_inbound_hmac_secret=form.sinch_inbound_hmac_secret; if (form.storage_backend) p.storage_backend=form.storage_backend; if (form.s3_bucket) p.s3_bucket=form.s3_bucket; if (form.s3_region) p.s3_region=form.s3_region; if (form.s3_prefix) p.s3_prefix=form.s3_prefix; if (form.s3_endpoint_url) p.s3_endpoint_url=form.s3_endpoint_url; if (form.s3_kms_key_id) p.s3_kms_key_id=form.s3_kms_key_id; if (form.max_file_size_mb!==undefined) p.max_file_size_mb=Number(form.max_file_size_mb); if (form.max_requests_per_minute!==undefined) p.max_requests_per_minute=Number(form.max_requests_per_minute); if (form.inbound_list_rpm!==undefined) p.inbound_list_rpm=Number(form.inbound_list_rpm); if (form.inbound_get_rpm!==undefined) p.inbound_get_rpm=Number(form.inbound_get_rpm); const res = await client.updateSettings(p); await client.reloadSettings(); await fetchSettings(); setSnack('Settings applied and reloaded'); if (res && res._meta && res._meta.restart_recommended) setRestartHint(true); if (p.enable_persisted_settings!==undefined) setPersistedEnabled(!!p.enable_persisted_settings); } catch(e:any){ setError(e?.message||'Failed to apply settings'); } finally { setLoading(false);} }} disabled={loading}>
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
