import React, { useState, useEffect } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Button,
  Alert,
  CircularProgress,
  Grid,
  Chip,
  TextField,
  MenuItem,
  FormControlLabel,
  Switch,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  IconButton,
  Tooltip,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Stack,
} from '@mui/material';
import {
  Settings as SettingsIcon,
  Refresh as RefreshIcon,
  Edit as EditIcon,
  Visibility as VisibilityIcon,
  VisibilityOff as VisibilityOffIcon,
  ExpandMore as ExpandMoreIcon,
  Cached as CacheIcon,
  Save as SaveIcon,
  Cancel as CancelIcon,
  Security as SecurityIcon,
  Storage as StorageIcon,
  Person as PersonIcon,
  Business as BusinessIcon,
  Group as GroupIcon,
  Public as PublicIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';

interface ConfigurationManagerProps {
  client: AdminAPIClient;
}

interface ConfigValue {
  value: any;
  source: 'db' | 'env' | 'default' | 'cache';
  level?: string;
  level_id?: string;
  encrypted?: boolean;
  updated_at?: string;
}

interface ConfigLayer {
  value: any;
  source: 'db' | 'env' | 'default' | 'cache';
  level: string;
  level_id?: string;
  encrypted: boolean;
  updated_at?: string;
}

const ConfigurationManager: React.FC<ConfigurationManagerProps> = ({ client }) => {
  const [effectiveConfig, setEffectiveConfig] = useState<Record<string, ConfigValue> | null>(null);
  const [safeKeys, setSafeKeys] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cacheStats, setCacheStats] = useState<any>(null);

  // Edit dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editingKey, setEditingKey] = useState<string>('');
  const [editingValue, setEditingValue] = useState<string>('');
  const [editingLevel, setEditingLevel] = useState<string>('global');
  const [editingLevelId, setEditingLevelId] = useState<string>('');
  const [editingReason, setEditingReason] = useState<string>('Admin panel update');
  const [editingEncrypt, setEditingEncrypt] = useState<boolean>(false);

  // Hierarchy dialog state
  const [hierarchyDialogOpen, setHierarchyDialogOpen] = useState(false);
  const [hierarchyKey, setHierarchyKey] = useState<string>('');
  const [hierarchyLayers, setHierarchyLayers] = useState<ConfigLayer[]>([]);
  const [hierarchyLoading, setHierarchyLoading] = useState(false);

  // Visibility state for masked values
  const [visibleSecrets, setVisibleSecrets] = useState<Set<string>>(new Set());

  const levels = [
    { value: 'global', label: 'Global', icon: <PublicIcon fontSize="small" /> },
    { value: 'tenant', label: 'Tenant', icon: <BusinessIcon fontSize="small" /> },
    { value: 'department', label: 'Department', icon: <GroupIcon fontSize="small" /> },
    { value: 'group', label: 'Group', icon: <GroupIcon fontSize="small" /> },
    { value: 'user', label: 'User', icon: <PersonIcon fontSize="small" /> },
  ];

  useEffect(() => {
    loadEffectiveConfig();
    loadSafeKeys();
  }, []);

  const loadEffectiveConfig = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await client.getEffectiveConfig();
      setEffectiveConfig(result.values || {});
      setCacheStats(result.cache_stats);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load configuration');
    } finally {
      setLoading(false);
    }
  };

  const loadSafeKeys = async () => {
    try {
      const keys = await client.getSafeEditKeys();
      setSafeKeys(keys);
    } catch (err) {
      console.warn('Failed to load safe edit keys:', err);
    }
  };

  const loadHierarchy = async (key: string) => {
    setHierarchyLoading(true);
    try {
      const result = await client.getConfigHierarchy(key);
      setHierarchyLayers(result.layers || []);
    } catch (err) {
      console.error('Failed to load hierarchy:', err);
      setHierarchyLayers([]);
    } finally {
      setHierarchyLoading(false);
    }
  };

  const handleEdit = (key: string, currentValue: ConfigValue) => {
    setEditingKey(key);
    setEditingValue(typeof currentValue.value === 'string' ? currentValue.value : JSON.stringify(currentValue.value, null, 2));
    setEditingLevel(currentValue.level || 'global');
    setEditingLevelId(currentValue.level_id || '');
    setEditingEncrypt(currentValue.encrypted || false);
    setEditDialogOpen(true);
  };

  const handleSave = async () => {
    try {
      let parsedValue: any = editingValue;
      try {
        parsedValue = JSON.parse(editingValue);
      } catch {
        // Keep as string if not valid JSON
      }

      await client.setConfigValue(
        editingKey,
        parsedValue,
        editingLevel,
        editingLevelId || undefined,
        editingReason,
        editingEncrypt
      );

      setEditDialogOpen(false);
      await loadEffectiveConfig(); // Refresh the view
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save configuration');
    }
  };

  const handleViewHierarchy = async (key: string) => {
    setHierarchyKey(key);
    setHierarchyDialogOpen(true);
    await loadHierarchy(key);
  };

  const handleFlushCache = async () => {
    try {
      await client.flushConfigCache();
      await loadEffectiveConfig(); // Refresh after cache flush
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to flush cache');
    }
  };

  const toggleSecretVisibility = (key: string) => {
    const newVisible = new Set(visibleSecrets);
    if (newVisible.has(key)) {
      newVisible.delete(key);
    } else {
      newVisible.add(key);
    }
    setVisibleSecrets(newVisible);
  };

  const getSourceChip = (source: string) => {
    const sourceConfig = {
      db: { color: 'primary' as const, label: 'Database', icon: <StorageIcon fontSize="small" /> },
      cache: { color: 'secondary' as const, label: 'Cache', icon: <CacheIcon fontSize="small" /> },
      env: { color: 'warning' as const, label: 'Environment', icon: <SettingsIcon fontSize="small" /> },
      default: { color: 'default' as const, label: 'Default', icon: <PublicIcon fontSize="small" /> },
    };
    const config = sourceConfig[source as keyof typeof sourceConfig] || sourceConfig.default;

    return (
      <Chip
        size="small"
        color={config.color}
        icon={config.icon}
        label={config.label}
        variant="outlined"
      />
    );
  };

  const getLevelChip = (level?: string) => {
    if (!level) return null;
    const levelConfig = levels.find(l => l.value === level);
    if (!levelConfig) return <Chip size="small" label={level} />;

    return (
      <Chip
        size="small"
        icon={levelConfig.icon}
        label={levelConfig.label}
        variant="filled"
        color="info"
      />
    );
  };

  const renderValue = (key: string, configValue: ConfigValue) => {
    const isSecret = configValue.encrypted || key.toLowerCase().includes('secret') || key.toLowerCase().includes('key');
    const isVisible = visibleSecrets.has(key);

    if (isSecret && !isVisible) {
      return (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
            ••••••••••••
          </Typography>
          <IconButton size="small" onClick={() => toggleSecretVisibility(key)}>
            <VisibilityIcon fontSize="small" />
          </IconButton>
        </Box>
      );
    }

    const displayValue = typeof configValue.value === 'object'
      ? JSON.stringify(configValue.value, null, 2)
      : String(configValue.value);

    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <Typography variant="body2" sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
          {displayValue}
        </Typography>
        {isSecret && (
          <IconButton size="small" onClick={() => toggleSecretVisibility(key)}>
            <VisibilityOffIcon fontSize="small" />
          </IconButton>
        )}
      </Box>
    );
  };

  if (loading && !effectiveConfig) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: 200 }}>
        <CircularProgress />
      </Box>
    );
  }

  return (
    <Box sx={{ p: 3 }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
        <Typography variant="h4" gutterBottom>
          Configuration Manager
        </Typography>
        <Stack direction="row" spacing={2}>
          <Button
            variant="outlined"
            startIcon={<CacheIcon />}
            onClick={handleFlushCache}
            disabled={loading}
          >
            Flush Cache
          </Button>
          <Button
            variant="contained"
            startIcon={<RefreshIcon />}
            onClick={loadEffectiveConfig}
            disabled={loading}
          >
            Refresh
          </Button>
        </Stack>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {cacheStats && (
        <Accordion sx={{ mb: 3 }}>
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Typography variant="h6">Cache Statistics</Typography>
          </AccordionSummary>
          <AccordionDetails>
            <Typography variant="body2" sx={{ fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>
              {JSON.stringify(cacheStats, null, 2)}
            </Typography>
          </AccordionDetails>
        </Accordion>
      )}

      <Card>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            Effective Configuration Values
          </Typography>

          {effectiveConfig && Object.keys(effectiveConfig).length > 0 ? (
            <TableContainer component={Paper} variant="outlined">
              <Table>
                <TableHead>
                  <TableRow>
                    <TableCell>Key</TableCell>
                    <TableCell>Value</TableCell>
                    <TableCell>Source</TableCell>
                    <TableCell>Level</TableCell>
                    <TableCell>Actions</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {Object.entries(effectiveConfig).map(([key, configValue]) => (
                    <TableRow key={key}>
                      <TableCell>
                        <Typography variant="body2" sx={{ fontFamily: 'monospace', fontWeight: 'bold' }}>
                          {key}
                        </Typography>
                        {configValue.encrypted && (
                          <Chip size="small" icon={<SecurityIcon fontSize="small" />} label="Encrypted" color="warning" />
                        )}
                      </TableCell>
                      <TableCell sx={{ maxWidth: 300 }}>
                        {renderValue(key, configValue)}
                      </TableCell>
                      <TableCell>
                        {getSourceChip(configValue.source)}
                      </TableCell>
                      <TableCell>
                        {getLevelChip(configValue.level)}
                        {configValue.level_id && (
                          <Typography variant="caption" display="block" color="textSecondary">
                            {configValue.level_id}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell>
                        <Stack direction="row" spacing={1}>
                          <Tooltip title="View hierarchy">
                            <IconButton size="small" onClick={() => handleViewHierarchy(key)}>
                              <VisibilityIcon fontSize="small" />
                            </IconButton>
                          </Tooltip>
                          {(safeKeys[key] || configValue.encrypted) && (
                            <Tooltip title="Edit value">
                              <IconButton size="small" onClick={() => handleEdit(key, configValue)}>
                                <EditIcon fontSize="small" />
                              </IconButton>
                            </Tooltip>
                          )}
                        </Stack>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          ) : (
            <Typography color="textSecondary">
              No configuration values found.
            </Typography>
          )}
        </CardContent>
      </Card>

      {/* Edit Dialog */}
      <Dialog open={editDialogOpen} onClose={() => setEditDialogOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle>Edit Configuration: {editingKey}</DialogTitle>
        <DialogContent>
          <Grid container spacing={2} sx={{ mt: 1 }}>
            <Grid item xs={12}>
              <TextField
                label="Value"
                multiline
                rows={4}
                value={editingValue}
                onChange={(e) => setEditingValue(e.target.value)}
                fullWidth
                variant="outlined"
              />
            </Grid>
            <Grid item xs={6}>
              <TextField
                select
                label="Level"
                value={editingLevel}
                onChange={(e) => setEditingLevel(e.target.value)}
                fullWidth
              >
                {levels.map((level) => (
                  <MenuItem key={level.value} value={level.value}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      {level.icon}
                      {level.label}
                    </Box>
                  </MenuItem>
                ))}
              </TextField>
            </Grid>
            <Grid item xs={6}>
              <TextField
                label="Level ID (if applicable)"
                value={editingLevelId}
                onChange={(e) => setEditingLevelId(e.target.value)}
                fullWidth
                placeholder="e.g., tenant_id, user_id"
              />
            </Grid>
            <Grid item xs={12}>
              <TextField
                label="Reason for change"
                value={editingReason}
                onChange={(e) => setEditingReason(e.target.value)}
                fullWidth
              />
            </Grid>
            <Grid item xs={12}>
              <FormControlLabel
                control={
                  <Switch
                    checked={editingEncrypt}
                    onChange={(e) => setEditingEncrypt(e.target.checked)}
                  />
                }
                label="Encrypt value"
              />
            </Grid>
          </Grid>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEditDialogOpen(false)} startIcon={<CancelIcon />}>
            Cancel
          </Button>
          <Button onClick={handleSave} variant="contained" startIcon={<SaveIcon />}>
            Save
          </Button>
        </DialogActions>
      </Dialog>

      {/* Hierarchy Dialog */}
      <Dialog open={hierarchyDialogOpen} onClose={() => setHierarchyDialogOpen(false)} maxWidth="lg" fullWidth>
        <DialogTitle>Configuration Hierarchy: {hierarchyKey}</DialogTitle>
        <DialogContent>
          {hierarchyLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
              <CircularProgress />
            </Box>
          ) : (
            <TableContainer component={Paper} variant="outlined" sx={{ mt: 2 }}>
              <Table>
                <TableHead>
                  <TableRow>
                    <TableCell>Priority</TableCell>
                    <TableCell>Level</TableCell>
                    <TableCell>Level ID</TableCell>
                    <TableCell>Value</TableCell>
                    <TableCell>Source</TableCell>
                    <TableCell>Updated</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {hierarchyLayers.map((layer, index) => (
                    <TableRow key={index} sx={{ bgcolor: index === 0 ? 'action.selected' : 'inherit' }}>
                      <TableCell>
                        <Chip size="small" label={index + 1} color={index === 0 ? 'primary' : 'default'} />
                        {index === 0 && <Typography variant="caption" display="block">Effective</Typography>}
                      </TableCell>
                      <TableCell>
                        {getLevelChip(layer.level)}
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                          {layer.level_id || '-'}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                          {typeof layer.value === 'object' ? JSON.stringify(layer.value) : String(layer.value)}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        {getSourceChip(layer.source)}
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption">
                          {layer.updated_at ? new Date(layer.updated_at).toLocaleString() : '-'}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setHierarchyDialogOpen(false)}>
            Close
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default ConfigurationManager;