import { useEffect, useState } from 'react';
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Button,
  Alert,
  Typography,
  Box,
  Checkbox,
  FormControlLabel,
} from '@mui/material';

type PluginItem = {
  id: string;
  name: string;
  version?: string;
  categories?: string[];
};

interface Props {
  open: boolean;
  plugin: PluginItem | null;
  initialConfig?: { enabled?: boolean; settings?: any } | null;
  onClose: () => void;
  onSave: (config: { enabled?: boolean; settings?: any }) => Promise<void>;
}

export default function PluginConfigDialog({ open, plugin, initialConfig, onClose, onSave }: Props) {
  const [config, setConfig] = useState<Record<string, any>>({});
  const [enabled, setEnabled] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    setError('');
    setSaving(false);
    setEnabled(initialConfig?.enabled ?? true);
    setConfig({ ...(initialConfig?.settings || {}) });
  }, [initialConfig, plugin]);

  const help = (text: string) => (
    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>{text}</Typography>
  );

  const renderFields = () => {
    const pid = (plugin?.id || '').toLowerCase();
    if (!pid) return null;

    if (pid === 'phaxio') {
      return (
        <Box>
          <Alert severity="info" sx={{ mb: 2 }}>
            Secrets (API key/secret) are configured in Settings → Backend: Phaxio. This form stores only non‑secret values in the plugin config.
          </Alert>
          <TextField
            label="Callback URL"
            fullWidth
            size="small"
            value={config.callback_url || ''}
            onChange={(e) => setConfig({ ...config, callback_url: e.target.value })}
            margin="normal"
          />
          {help('Example: https://yourdomain.com/phaxio-callback')}
          <FormControlLabel
            control={<Checkbox checked={!!config.verify_signature} onChange={(e) => setConfig({ ...config, verify_signature: e.target.checked })} />}
            label="Verify inbound signatures"
          />
        </Box>
      );
    }

    if (pid === 'sinch') {
      return (
        <Box>
          <Alert severity="info" sx={{ mb: 2 }}>
            Secrets (API key/secret) are configured in Settings → Backend: Sinch. This form stores only non‑secret values in the plugin config.
          </Alert>
          <TextField
            label="Project ID"
            fullWidth
            size="small"
            value={config.project_id || ''}
            onChange={(e) => setConfig({ ...config, project_id: e.target.value })}
            margin="normal"
          />
        </Box>
      );
    }

    if (pid === 'sip') {
      return (
        <Box>
          <Alert severity="info">
            Configure AMI host/port/credentials and Station ID in Settings → Backend: SIP/Asterisk. No additional non‑secret plugin settings are required here.
          </Alert>
        </Box>
      );
    }

    if (pid === 's3') {
      return (
        <Box>
          <TextField
            label="Bucket"
            fullWidth
            size="small"
            value={config.bucket || ''}
            onChange={(e) => setConfig({ ...config, bucket: e.target.value })}
            margin="normal"
          />
          <TextField
            label="Region"
            fullWidth
            size="small"
            value={config.region || ''}
            onChange={(e) => setConfig({ ...config, region: e.target.value })}
            margin="normal"
          />
          <TextField
            label="Prefix"
            fullWidth
            size="small"
            value={config.prefix || ''}
            onChange={(e) => setConfig({ ...config, prefix: e.target.value })}
            margin="normal"
          />
          <TextField
            label="Endpoint URL"
            fullWidth
            size="small"
            value={config.endpoint_url || ''}
            onChange={(e) => setConfig({ ...config, endpoint_url: e.target.value })}
            margin="normal"
          />
          <TextField
            label="KMS Key ID"
            fullWidth
            size="small"
            value={config.kms_key_id || ''}
            onChange={(e) => setConfig({ ...config, kms_key_id: e.target.value })}
            margin="normal"
          />
          {help('Secrets/credentials are configured via your runtime environment or role, not here.')}
        </Box>
      );
    }

    return (
      <Alert severity="info">
        No configurable fields for this plugin.
      </Alert>
    );
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      setError('');
      await onSave({ enabled, settings: config });
      onClose();
    } catch (e: any) {
      setError(e?.message || 'Failed to save configuration');
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
          Settings are saved to the Faxbot plugin config file.
        </Typography>
        <Box sx={{ mb: 2 }}>
          <FormControlLabel control={<Checkbox checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />} label="Enable plugin" />
        </Box>
        {renderFields()}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

