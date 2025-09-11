import { useState, useEffect } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Grid,
  Chip,
  Button,
  CircularProgress,
  Alert,
  Tooltip,
} from '@mui/material';
import {
  Refresh as RefreshIcon,
  CheckCircle as CheckCircleIcon,
  Warning as WarningIcon,
  Error as ErrorIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { HealthStatus } from '../api/types';

interface DashboardProps {
  client: AdminAPIClient;
  onNavigate?: (tabIndex: number) => void;
}

function Dashboard({ client, onNavigate }: DashboardProps) {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [justApplied, setJustApplied] = useState<boolean>(false);

  const fetchHealth = async () => {
    try {
      setError(null);
      const data = await client.getHealthStatus();
      setHealth(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch health status');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchHealth();
    if (sessionStorage.getItem('fb_admin_applied') === '1') {
      setJustApplied(true);
      sessionStorage.removeItem('fb_admin_applied');
      setTimeout(() => setJustApplied(false), 4000);
    }
    
    // Start polling
    const cleanup = client.startPolling((data) => {
      setHealth(data);
      setError(null);
    });
    
    return cleanup;
  }, [client]);

  const getStatusColor = (healthy: boolean) => {
    return healthy ? 'success' : 'error';
  };

  const getStatusIcon = (healthy: boolean) => {
    return healthy ? <CheckCircleIcon /> : <ErrorIcon />;
  };

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" minHeight={200}>
        <CircularProgress />
      </Box>
    );
  }

  return (
    <Box>
      {justApplied && (
        <Alert severity="success" sx={{ mb: 2 }}>
          Configuration applied successfully.
        </Alert>
      )}
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          Dashboard
        </Typography>
        <Button
          variant="outlined"
          startIcon={<RefreshIcon />}
          onClick={fetchHealth}
          disabled={loading}
        >
          Refresh
        </Button>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      {health && (
        <Grid container spacing={{ xs: 2, md: 3 }}>
          {/* System Status */}
          <Grid item xs={12} sm={6} lg={3}>
            <Tooltip title="Click to view detailed diagnostics" arrow>
              <Card 
                sx={{ 
                  cursor: 'pointer',
                  '&:hover': {
                    backgroundColor: 'rgba(59, 160, 255, 0.08)',
                    transform: 'translateY(-2px)',
                    boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                  },
                  transition: 'all 0.2s ease-in-out',
                }}
                onClick={() => onNavigate?.(7)} // Navigate to Diagnostics tab (index 7)
              >
              <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                <Box display="flex" alignItems="center" mb={{ xs: 1, sm: 2 }}>
                  {getStatusIcon(health.backend_healthy)}
                  <Typography variant="h6" component="h2" sx={{ ml: 1, fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                    System Status
                  </Typography>
                </Box>
                <Chip
                  label={health.backend_healthy ? 'Healthy' : 'Unhealthy'}
                  color={getStatusColor(health.backend_healthy)}
                  variant="outlined"
                />
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  Backend: {health.backend}
                </Typography>
              </CardContent>
              </Card>
            </Tooltip>
          </Grid>

          {/* Job Queue */}
          <Grid item xs={12} sm={6} lg={3}>
            <Tooltip title="Click to view all jobs" arrow>
              <Card 
                sx={{ 
                  cursor: 'pointer',
                  '&:hover': {
                    backgroundColor: 'rgba(59, 160, 255, 0.08)',
                    transform: 'translateY(-2px)',
                    boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                  },
                  transition: 'all 0.2s ease-in-out',
                }}
                onClick={() => onNavigate?.(2)} // Navigate to Jobs tab (index 2)
              >
              <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                <Typography variant="h6" component="h2" gutterBottom sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                  Job Queue
                </Typography>
                <Box display="flex" flexDirection="column" gap={1}>
                  <Box display="flex" justifyContent="space-between">
                    <Typography variant="body2">Queued:</Typography>
                    <Typography variant="body2" fontWeight="bold">
                      {health.jobs.queued}
                    </Typography>
                  </Box>
                  <Box display="flex" justifyContent="space-between">
                    <Typography variant="body2">In Progress:</Typography>
                    <Typography variant="body2" fontWeight="bold">
                      {health.jobs.in_progress}
                    </Typography>
                  </Box>
                  <Box display="flex" justifyContent="space-between">
                    <Typography variant="body2">Recent Failures:</Typography>
                    <Typography 
                      variant="body2" 
                      fontWeight="bold"
                      color={health.jobs.recent_failures > 0 ? 'error' : 'text.primary'}
                    >
                      {health.jobs.recent_failures}
                    </Typography>
                  </Box>
                </Box>
              </CardContent>
              </Card>
            </Tooltip>
          </Grid>

          {/* Inbound Status */}
          <Grid item xs={12} sm={6} lg={3}>
            <Tooltip title="Click to view inbound faxes" arrow>
              <Card 
                sx={{ 
                  cursor: 'pointer',
                  '&:hover': {
                    backgroundColor: 'rgba(59, 160, 255, 0.08)',
                    transform: 'translateY(-2px)',
                    boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                  },
                  transition: 'all 0.2s ease-in-out',
                }}
                onClick={() => onNavigate?.(3)} // Navigate to Inbound tab (index 3)
              >
                <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                  <Typography variant="h6" component="h2" gutterBottom sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                    Inbound Fax
                  </Typography>
                  <Chip
                    label={health.inbound_enabled ? 'Enabled' : 'Disabled'}
                    color={health.inbound_enabled ? 'success' : 'warning'}
                    variant="outlined"
                  />
                </CardContent>
              </Card>
            </Tooltip>
          </Grid>

          {/* Security Status */}
          <Grid item xs={12} sm={6} lg={3}>
            <Tooltip title="Click to view security settings" arrow>
              <Card 
                sx={{ 
                  cursor: 'pointer',
                  '&:hover': {
                    backgroundColor: 'rgba(59, 160, 255, 0.08)',
                    transform: 'translateY(-2px)',
                    boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                  },
                  transition: 'all 0.2s ease-in-out',
                }}
                onClick={() => onNavigate?.(6)} // Navigate to Settings tab (index 6)
              >
                <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                  <Typography variant="h6" component="h2" gutterBottom sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                    Security
                  </Typography>
                  <Box display="flex" flexDirection="column" gap={1}>
                    <Box display="flex" alignItems="center">
                      {health.require_auth ? <CheckCircleIcon color="success" /> : <WarningIcon color="warning" />}
                      <Typography variant="body2" sx={{ ml: 1 }}>
                        {health.require_auth ? 'Auth Required' : 'Auth Optional'}
                      </Typography>
                    </Box>
                    <Box display="flex" alignItems="center">
                      {health.api_keys_configured ? <CheckCircleIcon color="success" /> : <ErrorIcon color="error" />}
                      <Typography variant="body2" sx={{ ml: 1 }}>
                        {health.api_keys_configured ? 'API Keys Configured' : 'No API Keys'}
                      </Typography>
                    </Box>
                  </Box>
                </CardContent>
              </Card>
            </Tooltip>
          </Grid>

          {/* Last Updated */}
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography variant="body2" color="text.secondary">
                  Last updated: {new Date(health.timestamp).toLocaleString()}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  Auto-refreshing every 5 seconds
                </Typography>
              </CardContent>
            </Card>
          </Grid>
        </Grid>
      )}
    </Box>
  );
}

export default Dashboard;
