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
import type { PluginConfiguration, PluginConfigurationPatch } from '../api/types';

type PluginItem = {
  id: string;
  name: string;
  version?: string;
  categories?: string[];
};

interface Props {
  open: boolean;
  plugin: PluginItem | null;
  initialConfig: PluginConfiguration | null;
  loading: boolean;
  loadError: string;
  onClose: () => void;
  onReload: () => void;
  onSave: (config: PluginConfigurationPatch) => Promise<void>;
}

export default function PluginConfigDialog({ open, plugin, initialConfig, loading, loadError, onClose, onReload, onSave }: Props) {
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    setError('');
    setSaving(false);
    setEnabled(initialConfig?.enabled ?? false);
    setConfig({ ...(initialConfig?.settings || {}) });
  }, [initialConfig, plugin]);

  const ready = !loading && !loadError && !!initialConfig?._meta?.desired_revision_id;
  const changedSettings = Object.fromEntries(Object.entries(config)
    .filter(([key, value]) => !Object.is(value, initialConfig?.settings[key])));
  const enabledChanged = initialConfig !== null && enabled !== initialConfig.enabled;
  const changed = enabledChanged || Object.keys(changedSettings).length > 0;

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
            Configure API credentials in Settings → Backend: Phaxio. This form updates the same desired revision and preserves fields you leave unchanged.
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
          <TextField
            label="Callback Token"
            type="password"
            fullWidth
            size="small"
            value={config.callback_token || ''}
            onChange={(e) => setConfig({ ...config, callback_token: e.target.value })}
            margin="normal"
            helperText="Separate Callback Token from the Phaxio console. Leave unchanged to preserve it."
          />
          <FormControlLabel
            control={<Checkbox checked={!!config.verify_signature} onChange={(e) => setConfig({ ...config, verify_signature: e.target.checked })} />}
            label="Verify outbound status signatures"
          />
        </Box>
      );
    }

    if (pid === 'sinch') {
      return (
        <Box>
          <Alert severity="info" sx={{ mb: 2 }}>
            Configure API credentials in Settings → Backend: Sinch. This form updates the same desired revision and preserves fields you leave unchanged.
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
          {help('Existing credentials are preserved when you change these storage settings.')}
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
    if (!ready || !initialConfig) {
      setError('Reload a canonical plugin revision before saving.');
      return;
    }
    try {
      setSaving(true);
      setError('');
      const patch: PluginConfigurationPatch = {
        expected_revision_id: initialConfig._meta.desired_revision_id,
        role: initialConfig.role,
      };
      if (enabledChanged) patch.enabled = enabled;
      // Empty settings resets built-ins. Omission preserves untouched values,
      // including opaque secret masks and fields absent from this form.
      if (Object.keys(changedSettings).length > 0) patch.settings = changedSettings;
      await onSave(patch);
      onClose();
    } catch (e: any) {
      setError(e?.message || 'Failed to save configuration');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onClose={() => { if (!saving) onClose(); }} maxWidth="sm" fullWidth>
      <DialogTitle>Configure {plugin?.name}</DialogTitle>
      <DialogContent>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        {loadError && <Alert severity="error" sx={{ mb: 2 }}>{loadError}</Alert>}
        {loading && <Alert severity="info" sx={{ mb: 2 }}>Loading desired plugin settings…</Alert>}
        {initialConfig && <Alert severity={initialConfig._meta.apply_state === 'pending_restart' ? 'warning' : 'info'} sx={{ mb: 2 }}>
          {initialConfig._meta.apply_state === 'pending_restart'
            ? 'Editing the desired revision. Active behavior continues until a full installation restart.'
            : 'Editing the applied revision. Changes save durably; hot changes activate immediately.'}
          <Typography variant="caption" display="block" sx={{ mt: 1 }}>
            Desired revision: {initialConfig._meta.desired_revision_id}. Active revision: {initialConfig._meta.active_revision_id}.
          </Typography>
        </Alert>}
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Saving updates the desired installation configuration. Unchanged fields preserve their existing values.
        </Typography>
        {ready && <Box component="fieldset" disabled={saving} sx={{ border: 0, p: 0, m: 0 }}>
          <Box sx={{ mb: 2 }}>
            <FormControlLabel control={<Checkbox checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />} label={`Use this provider for ${initialConfig?.role}`} />
            {help('Selecting this provider replaces the desired selection for this role. Unchecking the selected provider disables the role.')}
          </Box>
          {renderFields()}
        </Box>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={saving}>Cancel</Button>
        <Button onClick={onReload} disabled={loading || saving}>Reload settings</Button>
        <Button variant="contained" onClick={handleSave} disabled={saving || !ready || !changed}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
