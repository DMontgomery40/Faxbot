import { useState, useEffect } from 'react';
import {
  Box,
  Paper,
  Typography,
  Button,
  Alert,
  Grid,
  Chip,
  CircularProgress,
  Card,
  CardContent,
  Stack,
  TextField,
  MenuItem,
  Link,
  useTheme,
  useMediaQuery,
  Tooltip,
  IconButton
} from '@mui/material';
import {
  Refresh as RefreshIcon,
  Storage as StorageIcon,
  CloudQueue as CloudIcon,
  Settings as SettingsIcon,
  Visibility as VisibilityIcon,
  VisibilityOff as VisibilityOffIcon,
  Info as InfoIcon,
  FlashOn as FlashOnIcon
} from '@mui/icons-material';

import AdminAPIClient from '../api/client';

interface ConfigurationManagerProps {
  client: AdminAPIClient;
  docsBase?: string;
}

interface ConfigItem {
  key: string;
  value: any;
  source: 'db' | 'env' | 'default' | 'cache' | null;
  level?: string;
}

interface HierarchyLevel {
  user: any;
  group: any;
  department: any;
  tenant: any;
  global: any;
  env: any;
  default: any;
}

interface HierarchyData {
  key: string;
  levels: HierarchyLevel;
  effective: ConfigItem;
}

// All available configuration keys organized by category
const CONFIG_CATEGORIES = {
  System: [
    'system.public_api_url',
  ],
  API: [
    'api.rate_limit_rpm',
    'api.session_timeout_hours',
  ],
  Security: [
    'security.enforce_public_https',
    'security.require_mfa',
    'security.password_min_length',
  ],
  Storage: [
    'storage.s3.bucket',
    'storage.s3.region',
    'storage.s3.endpoint_url',
  ],
  Fax: [
    'fax.timeout_seconds',
    'fax.max_pages',
    'fax.retry_attempts',
  ],
  Provider: [
    'provider.health_check_interval',
    'provider.circuit_breaker_threshold',
    'provider.circuit_breaker_timeout',
  ],
  Webhook: [
    'webhook.verify_signatures',
  ],
  Compliance: [
    'hipaa.enforce_compliance',
    'audit.retention_days',
  ],
};

const ALL_KEYS = Object.values(CONFIG_CATEGORIES).flat();

function ConfigurationManager({ client, docsBase }: ConfigurationManagerProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [effectiveConfig, setEffectiveConfig] = useState<Record<string, ConfigItem>>({});
  const [hierarchyData, setHierarchyData] = useState<Record<string, HierarchyData>>({});
  const [safeKeys, setSafeKeys] = useState<Record<string, any>>({});
  const [selectedKey, setSelectedKey] = useState<string>('');
  const [showValues, setShowValues] = useState<Record<string, boolean>>({});
  const [selectedCategory, setSelectedCategory] = useState<string>('All');
  const [searchTerm, setSearchTerm] = useState<string>('');
  const [cacheStats] = useState<any>({ backend: 'memory', memory_items: 0 });

  // Filter keys based on search and category
  const filteredKeys = ALL_KEYS.filter(key => {
    const matchesSearch = searchTerm === '' || key.toLowerCase().includes(searchTerm.toLowerCase());
    const matchesCategory = selectedCategory === 'All' ||
      Object.entries(CONFIG_CATEGORIES).some(([cat, keys]) =>
        cat === selectedCategory && keys.includes(key)
      );
    return matchesSearch && matchesCategory;
  });

  // Check if user has admin role for configuration management
  // TODO: Implement proper role-based access control with userTraits
  const hasConfigAccess = true;

  const maskSensitive = (value: any, key: string): string => {
    if (value === null || value === undefined) return 'Not set';
    const str = String(value);

    // Mask potentially sensitive keys
    if (key.includes('key') || key.includes('secret') || key.includes('password') || key.includes('token')) {
      if (str.length <= 4) return '*'.repeat(str.length);
      return str.substring(0, 4) + '*'.repeat(Math.max(0, str.length - 4));
    }

    return str;
  };

  const getSourceIcon = (source: string | null) => {
    switch (source) {
      case 'db': return <StorageIcon fontSize="small" />;
      case 'env': return <CloudIcon fontSize="small" />;
      case 'default': return <SettingsIcon fontSize="small" />;
      case 'cache': return <FlashOnIcon fontSize="small" />;
      default: return <InfoIcon fontSize="small" />;
    }
  };

  const getSourceColor = (source: string | null): 'primary' | 'secondary' | 'default' | 'success' | 'warning' => {
    switch (source) {
      case 'db': return 'primary';
      case 'env': return 'warning';
      case 'default': return 'secondary';
      case 'cache': return 'success';
      default: return 'default';
    }
  };

  const fetchEffectiveConfig = async () => {
    try {
      setLoading(true);
      setError(null);

      // Fetch effective config for all keys
      const response = await client.v4GetEffective({ keys: ALL_KEYS });
      const configMap: Record<string, ConfigItem> = {};

      if (response.items) {
        Object.entries(response.items).forEach(([key, item]: [string, any]) => {
          configMap[key] = {
            key,
            value: item.value,
            source: item.source,
            level: item.level
          };
        });
      }

      setEffectiveConfig(configMap);

      // Cache stats would be fetched here if endpoint exists
      // For now, we'll skip this as it's not critical
    } catch (err: any) {
      console.error('Failed to fetch effective config:', err);
      setError(err.message || 'Failed to fetch configuration');
    } finally {
      setLoading(false);
    }
  };

  const fetchHierarchy = async (key: string) => {
    try {
      const response = await client.v4GetHierarchy({ key });
      setHierarchyData(prev => ({
        ...prev,
        [key]: response
      }));
    } catch (err: any) {
      console.error('Failed to fetch hierarchy for key:', key, err);
      // Don't set error for individual hierarchy failures
    }
  };

  const fetchSafeKeys = async () => {
    try {
      const response = await client.v4GetSafeKeys();
      setSafeKeys(response);
    } catch (err: any) {
      console.error('Failed to fetch safe keys:', err);
      // Non-critical, keep going
    }
  };

  const flushCache = async (scope: string = '*') => {
    try {
      setLoading(true);
      await client.v4FlushCache(scope);
      setSuccess('Cache flushed successfully');

      // Refresh data
      await fetchEffectiveConfig();

      setTimeout(() => setSuccess(null), 3000);
    } catch (err: any) {
      console.error('Failed to flush cache:', err);
      setError(err.message || 'Failed to flush cache');
    } finally {
      setLoading(false);
    }
  };

  const toggleShowValue = (key: string) => {
    setShowValues(prev => ({
      ...prev,
      [key]: !prev[key]
    }));
  };

  const handleKeySelect = (key: string) => {
    setSelectedKey(key);
    if (key && !hierarchyData[key]) {
      fetchHierarchy(key);
    }
  };

  useEffect(() => {
    if (hasConfigAccess) {
      fetchEffectiveConfig();
      fetchSafeKeys();
    }
  }, [hasConfigAccess]);

  // Clear messages after some time
  useEffect(() => {
    if (error) {
      const timer = setTimeout(() => setError(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [error]);

  if (!hasConfigAccess) {
    return (
      <Paper sx={{ p: 3, textAlign: 'center' }}>
        <Alert severity="warning" sx={{ mb: 2 }}>
          Configuration management requires admin role
        </Alert>
        <Typography color="text.secondary">
          Contact your administrator for access to hierarchical configuration settings.
        </Typography>
      </Paper>
    );
  }

  return (
    <Box sx={{ maxWidth: 'xl', mx: 'auto' }}>
      {/* Header */}
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 3, flexWrap: 'wrap', gap: 2 }}>
        <Box>
          <Typography variant="h4" gutterBottom sx={{ fontWeight: 600 }}>
            Configuration Manager
          </Typography>
          <Stack direction="row" spacing={2} alignItems="center">
            <Typography variant="body2" color="text.secondary">
              Hierarchical configuration with database-first resolution
            </Typography>
            {cacheStats && (
              <Chip
                icon={<FlashOnIcon />}
                label={`Cache: ${cacheStats.backend || 'memory'} (${cacheStats.memory_items || 0} items)`}
                size="small"
                color="success"
                variant="outlined"
              />
            )}
            {docsBase && (
              <Link
                href={`${docsBase}/configuration`}
                target="_blank"
                rel="noopener noreferrer"
                sx={{ textDecoration: 'none' }}
              >
                Learn more
              </Link>
            )}
          </Stack>
        </Box>
        <Stack direction="row" spacing={1}>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchEffectiveConfig}
            disabled={loading}
            size={isMobile ? 'small' : 'medium'}
          >
            Refresh
          </Button>
          <Button
            variant="contained"
            color="secondary"
            startIcon={<FlashOnIcon />}
            onClick={() => flushCache()}
            disabled={loading}
            size={isMobile ? 'small' : 'medium'}
          >
            Flush Cache
          </Button>
        </Stack>
      </Box>

      {/* Status Messages */}
      {error && (
        <Alert severity="error" sx={{ mb: 3 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert severity="success" sx={{ mb: 3 }} onClose={() => setSuccess(null)}>
          {success}
        </Alert>
      )}

      {loading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress />
        </Box>
      )}

      {/* Search and Filter Controls */}
      {!loading && (
        <Paper sx={{ p: 2, mb: 3 }}>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
            <TextField
              placeholder="Search configuration keys..."
              size="small"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              sx={{ flex: 1 }}
              InputProps={{
                startAdornment: <InfoIcon sx={{ mr: 1, color: 'text.secondary' }} />
              }}
            />
            <TextField
              select
              label="Category"
              value={selectedCategory}
              onChange={(e) => setSelectedCategory(e.target.value)}
              size="small"
              sx={{ minWidth: 150 }}
            >
              <MenuItem value="All">All Categories</MenuItem>
              {Object.keys(CONFIG_CATEGORIES).map(cat => (
                <MenuItem key={cat} value={cat}>{cat}</MenuItem>
              ))}
            </TextField>
            <Chip
              label={`${filteredKeys.length} keys`}
              color="primary"
              variant="outlined"
            />
          </Stack>
        </Paper>
      )}

      {!loading && (
        <Grid container spacing={3}>
          {/* Effective Configuration */}
          <Grid item xs={12} lg={6}>
            <Card sx={{ height: 'fit-content' }}>
              <CardContent>
                <Typography variant="h6" gutterBottom sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <StorageIcon />
                  Effective Configuration
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                  Current configuration values resolved from the hierarchy
                </Typography>

                <Stack spacing={2}>
                  {filteredKeys.map(key => {
                    const config = effectiveConfig[key];
                    const isSecret = key.includes('key') || key.includes('secret') || key.includes('password');
                    const showValue = showValues[key] || false;

                    return (
                      <Paper
                        key={key}
                        variant="outlined"
                        sx={{ p: 2, cursor: 'pointer' }}
                        onClick={() => handleKeySelect(key)}
                      >
                        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                          <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                            {key}
                          </Typography>
                          {config?.source && (
                            <Chip
                              icon={getSourceIcon(config.source)}
                              label={config.source?.toUpperCase() || 'UNKNOWN'}
                              size="small"
                              color={getSourceColor(config.source)}
                              variant="outlined"
                            />
                          )}
                        </Box>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                          <Typography
                            variant="body2"
                            color="text.secondary"
                            sx={{
                              fontFamily: 'monospace',
                              flex: 1,
                              wordBreak: 'break-all'
                            }}
                          >
                            {config ? (
                              isSecret && !showValue ?
                                maskSensitive(config.value, key) :
                                config.value || 'Not set'
                            ) : 'Loading...'}
                          </Typography>
                          {isSecret && config && (
                            <Tooltip title={showValue ? 'Hide value' : 'Show value'}>
                              <IconButton
                                size="small"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  toggleShowValue(key);
                                }}
                              >
                                {showValue ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
                              </IconButton>
                            </Tooltip>
                          )}
                        </Box>
                      </Paper>
                    );
                  })}
                </Stack>
              </CardContent>
            </Card>
          </Grid>

          {/* Hierarchy Detail */}
          <Grid item xs={12} lg={6}>
            <Card sx={{ height: 'fit-content' }}>
              <CardContent>
                <Typography variant="h6" gutterBottom sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <InfoIcon />
                  Configuration Hierarchy
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                  Resolution order: User → Group → Department → Tenant → Global → Environment → Default
                </Typography>

                {selectedKey ? (
                  <Box>
                    <TextField
                      select
                      fullWidth
                      label="Selected Configuration Key"
                      value={selectedKey}
                      onChange={(e) => handleKeySelect(e.target.value)}
                      sx={{ mb: 2 }}
                      size="small"
                    >
                      {ALL_KEYS.map(key => (
                        <MenuItem key={key} value={key}>
                          {key}
                        </MenuItem>
                      ))}
                    </TextField>

                    {hierarchyData[selectedKey] ? (
                      <Stack spacing={1}>
                        {Object.entries(hierarchyData[selectedKey].levels).map(([level, value]) => (
                          <Paper
                            key={level}
                            variant="outlined"
                            sx={{
                              p: 1.5,
                              opacity: value ? 1 : 0.6,
                              borderColor: value ? theme.palette.primary.main : theme.palette.divider
                            }}
                          >
                            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                              <Typography variant="subtitle2" sx={{ textTransform: 'capitalize', fontWeight: 600 }}>
                                {level}
                              </Typography>
                              {value && (
                                <Chip
                                  icon={getSourceIcon(level === 'env' ? 'env' : level === 'default' ? 'default' : 'db')}
                                  label="SET"
                                  size="small"
                                  color="primary"
                                  variant="filled"
                                />
                              )}
                            </Box>
                            <Typography
                              variant="body2"
                              color="text.secondary"
                              sx={{
                                fontFamily: 'monospace',
                                mt: 0.5,
                                wordBreak: 'break-all'
                              }}
                            >
                              {value ? String(value) : 'Not set at this level'}
                            </Typography>
                          </Paper>
                        ))}
                      </Stack>
                    ) : (
                      <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
                        <CircularProgress size={24} />
                      </Box>
                    )}
                  </Box>
                ) : (
                  <Paper
                    variant="outlined"
                    sx={{ p: 3, textAlign: 'center', borderStyle: 'dashed' }}
                  >
                    <Typography color="text.secondary">
                      Select a configuration key to view its hierarchy
                    </Typography>
                  </Paper>
                )}
              </CardContent>
            </Card>
          </Grid>
        </Grid>
      )}

      {/* Info Panel with Stats */}
      <Paper sx={{ mt: 3, p: 2, backgroundColor: theme.palette.action.hover }}>
        <Grid container spacing={2}>
          <Grid item xs={12} md={8}>
            <Typography variant="body2" color="text.secondary">
              <strong>Configuration Status:</strong> {Object.keys(safeKeys).length > 0 ? 'Edit mode available for safe keys' : 'Read-only mode'}.
              Resolution order: User → Group → Department → Tenant → Global → Environment → Default.
              {docsBase && (
                <>
                  {' '}
                  <Link
                    href={`${docsBase}/configuration/hierarchy`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    View documentation
                  </Link>
                </>
              )}
            </Typography>
          </Grid>
          <Grid item xs={12} md={4}>
            {cacheStats && (
              <Stack spacing={1}>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>Cache Statistics</Typography>
                <Typography variant="caption" color="text.secondary">
                  Backend: {cacheStats.backend}<br/>
                  Memory items: {cacheStats.memory_items || 0}<br/>
                  {cacheStats.redis_keys !== undefined && `Redis keys: ${cacheStats.redis_keys}`}
                </Typography>
              </Stack>
            )}
          </Grid>
        </Grid>
      </Paper>
    </Box>
  );
}

export default ConfigurationManager;