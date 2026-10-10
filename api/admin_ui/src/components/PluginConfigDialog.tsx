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
  const retainedDraft = initialConfig !== null && !ready;
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
            Set your Phaxio API key and secret in Settings → Backend: Phaxio.
          </Alert>
          <TextField
            label="Outbound Callback URL Override"
            fullWidth
            size="small"
            value={config.callback_url || ''}
            onChange={(e) => setConfig({ ...config, callback_url: e.target.value })}
            margin="normal"
          />
          {help('Leave empty to use /phaxio-callback on your public API URL.')}
          <TextField
            label="Callback Token"
            type="password"
            fullWidth
            size="small"
            value={config.callback_token || ''}
            onChange={(e) => setConfig({ ...config, callback_token: e.target.value })}
            margin="normal"
            helperText="Find this in the Phaxio console; it is required for authenticated callbacks."
          />
          <FormControlLabel
            control={<Checkbox checked={!!config.verify_signature} onChange={(e) => setConfig({ ...config, verify_signature: e.target.checked })} />}
            label="Enable authenticated outbound callbacks"
          />
          {help('When off, Faxbot ignores Phaxio callbacks and checks fax status by polling instead.')}
        </Box>
      );
    }

    if (pid === 'sinch') {
      return (
        <Box>
          <Alert severity="info" sx={{ mb: 2 }}>
            Set your Sinch API credentials in Settings → Backend: Sinch.
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
            Your carrier trunk has its own page under Delivery setup, named after your carrier. Set the name and number
            other fax machines see in Delivery setup → Sending identity.
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
          {help('Changing these settings keeps your saved storage credentials.')}
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
      setError('Reload settings before saving.');
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
        {error && error !== loadError && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        {loadError && <Alert severity="error" sx={{ mb: 2 }}>{loadError}</Alert>}
        {loading && <Alert severity="info" sx={{ mb: 2 }}>Loading settings…</Alert>}
        {initialConfig && !loading && !loadError && (retainedDraft || initialConfig._meta.apply_state === 'pending_restart') && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            {retainedDraft
              ? 'Your edits are kept here; reload to see the current values.'
              : 'Restart Faxbot to apply pending changes.'}
          </Alert>
        )}
        {initialConfig && <Box component="fieldset" disabled={saving || !ready} sx={{ border: 0, p: 0, m: 0 }}>
          <Box sx={{ mb: 2 }}>
            <FormControlLabel control={<Checkbox checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />} label={`Use this provider for ${initialConfig?.role}`} />
            {help('Selecting this replaces the current provider for this role; clearing it turns the role off.')}
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
