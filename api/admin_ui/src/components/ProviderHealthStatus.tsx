import React, { useState, useEffect } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Chip,
  Button,
  IconButton,
  Grid,
  Alert,
  CircularProgress,
  Tooltip,
  Stack,
} from '@mui/material';
import {
  Refresh as RefreshIcon,
  CheckCircle as HealthyIcon,
  Warning as DegradedIcon,
  Error as ErrorIcon,
  Block as DisabledIcon,
  PlayArrow as EnableIcon,
  Pause as DisableIcon,
  Timeline as ChartIcon,
} from '@mui/icons-material';
import { format } from 'date-fns';
import AdminAPIClient from '../api/client';

interface ProviderStatus {
  provider_id: string;
  provider_type: string;
  status: 'healthy' | 'degraded' | 'circuit_open' | 'disabled';
  failure_count: number;
  last_success: string | null;
  last_failure: string | null;
  next_retry_at?: string;
}

interface HealthStatusData {
  provider_statuses: Record<string, ProviderStatus>;
  total_providers: number;
  healthy_count: number;
  degraded_count: number;
  circuit_open_count: number;
  disabled_count: number;
}

interface ProviderHealthStatusProps {
  client: AdminAPIClient;
}

const ProviderHealthStatus: React.FC<ProviderHealthStatusProps> = ({ client }) => {

  const [healthData, setHealthData] = useState<HealthStatusData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const loadHealthStatus = async () => {
    setLoading(true);
    setError(null);

    try {
      const data = await client.getProviderHealthStatus();
      setHealthData(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load provider health status');
    } finally {
      setLoading(false);
    }
  };

  const handleProviderAction = async (providerId: string, action: 'enable' | 'disable') => {
    setActionLoading(providerId);

    try {
      if (action === 'enable') {
        await client.enableProvider(providerId);
      } else {
        await client.disableProvider(providerId);
      }

      // Refresh health status
      await loadHealthStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} provider`);
    } finally {
      setActionLoading(null);
    }
  };

  useEffect(() => {
    loadHealthStatus();

    // Auto-refresh every 30 seconds
    const interval = setInterval(loadHealthStatus, 30000);
    return () => clearInterval(interval);
  }, []);

  const getStatusIcon = (status: ProviderStatus['status']) => {
    switch (status) {
      case 'healthy':
        return <HealthyIcon color="success" />;
      case 'degraded':
        return <DegradedIcon color="warning" />;
      case 'circuit_open':
        return <ErrorIcon color="error" />;
      case 'disabled':
        return <DisabledIcon color="disabled" />;
      default:
        return <HealthyIcon />;
    }
  };

  const getStatusColor = (status: ProviderStatus['status']): "default" | "primary" | "secondary" | "error" | "info" | "success" | "warning" => {
    switch (status) {
      case 'healthy':
        return 'success';
      case 'degraded':
        return 'warning';
      case 'circuit_open':
        return 'error';
      case 'disabled':
        return 'default';
      default:
        return 'default';
    }
  };

  const getStatusText = (status: ProviderStatus['status']) => {
    switch (status) {
      case 'healthy':
        return 'Healthy';
      case 'degraded':
        return 'Degraded';
      case 'circuit_open':
        return 'Circuit Open';
      case 'disabled':
        return 'Disabled';
      default:
        return 'Unknown';
    }
  };

  const formatTimestamp = (timestamp: string | null) => {
    if (!timestamp) return 'Never';
    try {
      return format(new Date(timestamp), 'MMM dd, HH:mm:ss');
    } catch {
      return 'Invalid date';
    }
  };

  if (loading && !healthData) {
    return (
      <Card>
        <CardContent sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress />
        </CardContent>
      </Card>
    );
  }

  return (
    <Box>
      {/* Summary Cards */}
      {healthData && (
        <Grid container spacing={2} sx={{ mb: 3 }}>
          <Grid item xs={6} sm={3}>
            <Card>
              <CardContent sx={{ textAlign: 'center', py: 2 }}>
                <Typography variant="h4" color="text.secondary">
                  {healthData.total_providers}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Total Providers
                </Typography>
              </CardContent>
            </Card>
          </Grid>
          <Grid item xs={6} sm={3}>
            <Card>
              <CardContent sx={{ textAlign: 'center', py: 2 }}>
                <Typography variant="h4" color="success.main">
                  {healthData.healthy_count}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Healthy
                </Typography>
              </CardContent>
            </Card>
          </Grid>
          <Grid item xs={6} sm={3}>
            <Card>
              <CardContent sx={{ textAlign: 'center', py: 2 }}>
                <Typography variant="h4" color="warning.main">
                  {healthData.degraded_count}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Degraded
                </Typography>
              </CardContent>
            </Card>
          </Grid>
          <Grid item xs={6} sm={3}>
            <Card>
              <CardContent sx={{ textAlign: 'center', py: 2 }}>
                <Typography variant="h4" color="error.main">
                  {healthData.circuit_open_count + healthData.disabled_count}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Unavailable
                </Typography>
              </CardContent>
            </Card>
          </Grid>
        </Grid>
      )}

      {/* Controls */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
        <Typography variant="subtitle1" fontWeight={600}>
          Provider Details
        </Typography>
        <Tooltip title="Refresh health status">
          <IconButton onClick={loadHealthStatus} disabled={loading}>
            <RefreshIcon />
          </IconButton>
        </Tooltip>
      </Box>

      {/* Error Alert */}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Provider Details */}
      {healthData && Object.keys(healthData.provider_statuses).length === 0 ? (
        <Card>
          <CardContent sx={{ textAlign: 'center', py: 4, color: 'text.secondary' }}>
            <ChartIcon sx={{ fontSize: 48, mb: 1, opacity: 0.5 }} />
            <Typography variant="h6">No Providers Found</Typography>
            <Typography variant="body2">
              Health monitoring will start when providers are available
            </Typography>
          </CardContent>
        </Card>
      ) : (
        <Grid container spacing={2}>
          {healthData && Object.entries(healthData.provider_statuses).map(([providerId, status]) => (
            <Grid item xs={12} md={6} lg={4} key={providerId}>
              <Card>
                <CardContent>
                  <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      {getStatusIcon(status.status)}
                      <Typography variant="h6" noWrap>
                        {providerId}
                      </Typography>
                    </Box>
                    <Chip
                      label={getStatusText(status.status)}
                      color={getStatusColor(status.status)}
                      size="small"
                    />
                  </Box>

                  <Stack spacing={1} sx={{ mb: 2 }}>
                    <Typography variant="body2" color="text.secondary">
                      <strong>Type:</strong> {status.provider_type}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      <strong>Failures:</strong> {status.failure_count}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      <strong>Last Success:</strong> {formatTimestamp(status.last_success)}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      <strong>Last Failure:</strong> {formatTimestamp(status.last_failure)}
                    </Typography>
                    {status.next_retry_at && (
                      <Typography variant="body2" color="warning.main">
                        <strong>Next Retry:</strong> {formatTimestamp(status.next_retry_at)}
                      </Typography>
                    )}
                  </Stack>

                  <Stack direction="row" spacing={1}>
                    <Button
                      size="small"
                      variant="outlined"
                      color="success"
                      disabled={status.status === 'healthy' || actionLoading === providerId}
                      startIcon={actionLoading === providerId ? <CircularProgress size={16} /> : <EnableIcon />}
                      onClick={() => handleProviderAction(providerId, 'enable')}
                    >
                      Enable
                    </Button>
                    <Button
                      size="small"
                      variant="outlined"
                      color="error"
                      disabled={status.status === 'disabled' || actionLoading === providerId}
                      startIcon={<DisableIcon />}
                      onClick={() => handleProviderAction(providerId, 'disable')}
                    >
                      Disable
                    </Button>
                  </Stack>
                </CardContent>
              </Card>
            </Grid>
          ))}
        </Grid>
      )}
    </Box>
  );
};

export default ProviderHealthStatus;