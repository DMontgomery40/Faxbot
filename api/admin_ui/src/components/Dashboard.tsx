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
  useTheme,
} from '@mui/material';
import { darken } from '@mui/material/styles';
import {
  Refresh as RefreshIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  ContentCopy as ContentCopyIcon,
} from '@mui/icons-material';
import { IconButton } from '@mui/material';
import AdminAPIClient, { AdminAPIError, isNotAvailable } from '../api/client';
import type { HealthStatus } from '../api/types';
import type { DirectPartner, IntakeCounts, Money, ProviderCosts } from '../api/deliveryTypes';
import type { AdminDestination } from '../navigation';
import { formatMoney, formatMoneyList } from './delivery/shared';

type CardData<T> = { kind: 'loading' } | { kind: 'ready'; data: T } | { kind: 'denied' | 'unavailable' | 'error' };

async function settle<T>(request: Promise<T>): Promise<CardData<T>> {
  try {
    return { kind: 'ready', data: await request };
  } catch (error) {
    if (error instanceof AdminAPIError && (error.status === 401 || error.status === 403)) return { kind: 'denied' };
    return { kind: isNotAvailable(error) ? 'unavailable' : 'error' };
  }
}

const CARD_TEXT = {
  denied: 'Not available to this account.',
  unavailable: 'Not available on this server.',
  error: 'Could not load this. Select Refresh to try again.',
};

// Totals per currency; amounts are decimal strings.
function totalCost(providers: ProviderCosts[]): Money[] {
  const totals = new Map<string, number>();
  for (const provider of providers) {
    for (const cost of provider.estimated_cost) totals.set(cost.currency, (totals.get(cost.currency) ?? 0) + Number(cost.amount));
  }
  return [...totals].map(([currency, amount]) => ({ currency, amount: amount.toFixed(4) }));
}

const clickableCardSx = {
  cursor: 'pointer',
  height: '100%',
  '&:hover': {
    backgroundColor: 'rgba(59, 160, 255, 0.08)',
    transform: 'translateY(-2px)',
    boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
  },
  transition: 'all 0.2s ease-in-out',
};

// A dashboard card for one delivery area. It opens its screen only when its
// data is available to this account.
function DeliveryCard<T>({ title, hint, data, onOpen, children }: {
  title: string;
  hint: string;
  data: CardData<T>;
  onOpen?: () => void;
  children: (value: T) => React.ReactNode;
}) {
  const ready = data.kind === 'ready';
  const body = (
    <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
      <Typography variant="h6" component="h2" gutterBottom sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>{title}</Typography>
      {data.kind === 'loading' ? <CircularProgress size={20} aria-label={`Loading ${title}`} />
        : data.kind === 'ready' ? children(data.data)
        : <Typography variant="body2" color="text.secondary">{CARD_TEXT[data.kind]}</Typography>}
    </CardContent>
  );
  if (!ready || !onOpen) return <Card sx={{ height: '100%' }}>{body}</Card>;
  return (
    <Tooltip title={hint} arrow>
      <Card sx={clickableCardSx} onClick={onOpen} role="button" aria-label={title} tabIndex={0}
        onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpen(); } }}>
        {body}
      </Card>
    </Tooltip>
  );
}

function Line({ label, value, color }: { label: string; value: React.ReactNode; color?: string }) {
  return (
    <Box display="flex" justifyContent="space-between" gap={2}>
      <Typography variant="body2">{label}</Typography>
      <Typography variant="body2" fontWeight="bold" color={color ?? 'text.primary'} sx={{ textAlign: 'right' }}>{value}</Typography>
    </Box>
  );
}

interface DashboardProps {
  client: AdminAPIClient;
  onNavigate?: (destination: AdminDestination) => void;
}

function Dashboard({ client, onNavigate }: DashboardProps) {
  const theme = useTheme();
  const warningTextColor = theme.palette.mode === 'light'
    ? darken(theme.palette.warning.light, 0.6)
    : theme.palette.warning.main;
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [justApplied, setJustApplied] = useState<boolean>(false);
  const [cfg, setCfg] = useState<any | null>(null);
  const [plugins, setPlugins] = useState<any[] | null>(null);
  const [spending, setSpending] = useState<CardData<ProviderCosts[]>>({ kind: 'loading' });
  const [intake, setIntake] = useState<CardData<IntakeCounts>>({ kind: 'loading' });
  const [partners, setPartners] = useState<CardData<DirectPartner[]>>({ kind: 'loading' });

  // Delivery cards load on entry and on Refresh, not on every health poll.
  const fetchDelivery = async () => {
    const [costs, queue, peers] = await Promise.all([
      settle(client.getRouteCosts().then((result) => result.providers)),
      settle(client.listIntakeItems({ limit: 1 }).then((result) => result.counts)),
      settle(client.listDirectPartners().then((result) => result.peers)),
    ]);
    setSpending(costs);
    setIntake(queue);
    setPartners(peers);
  };

  const fetchHealth = async () => {
    try {
      setError(null);
      const data = await client.getHealthStatus();
      setHealth(data);
    } catch (err) {
      setError(err instanceof AdminAPIError && (err.status === 401 || err.status === 403)
        ? 'Server health is not available to this account.'
        : 'Could not load server health. Try again.');
    } finally {
      setLoading(false);
    }
  };

  const fetchConfig = async () => {
    try {
      const c = await client.getConfig();
      setCfg(c);
      // Lazy-load plugins list only when v3 plugins are enabled
      if (c?.v3_plugins?.enabled) {
        try {
          const list = await client.listPlugins();
          setPlugins(list?.items || []);
        } catch {
          setPlugins([]);
        }
      } else {
        setPlugins(null);
      }
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    fetchHealth();
    void fetchDelivery();
    if (sessionStorage.getItem('fb_admin_applied') === '1') {
      setJustApplied(true);
      sessionStorage.removeItem('fb_admin_applied');
      setTimeout(() => setJustApplied(false), 4000);
    }
    
    // Start polling
    const cleanup = client.startPolling((data) => {
      setHealth(data);
      setError(null);
      fetchConfig();
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
          onClick={() => { void fetchHealth(); void fetchDelivery(); }}
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
                onClick={() => onNavigate?.('diagnostics')}
              >
              <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                <Box display="flex" alignItems="center" mb={{ xs: 1, sm: 2 }}>
                  {getStatusIcon(health.backend_healthy)}
                  <Typography variant="h6" component="h2" sx={{ ml: 1, fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                    System Status
                  </Typography>
                </Box>
                <Chip
                  label={health.backend_healthy ? 'Ready' : 'Needs attention'}
                  color={getStatusColor(health.backend_healthy)}
                  variant="outlined"
                />
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  Outbound provider: {health.backend}
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
                onClick={() => onNavigate?.('jobs')}
              >
              <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                <Typography variant="h6" component="h2" gutterBottom sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                  Outbound Delivery
                </Typography>
                <Box display="flex" flexDirection="column" gap={1}>
                  <Box display="flex" justifyContent="space-between">
                    <Typography variant="body2">Ready / Preparing:</Typography>
                    <Typography variant="body2" fontWeight="bold">
                      {health.jobs.queued}
                    </Typography>
                  </Box>
                  <Box display="flex" justifyContent="space-between">
                    <Typography variant="body2">Submitting / In Progress:</Typography>
                    <Typography variant="body2" fontWeight="bold">
                      {health.jobs.in_progress}
                    </Typography>
                  </Box>
                  <Box display="flex" justifyContent="space-between">
                    <Typography variant="body2">Failures (Last 24 Hours):</Typography>
                    <Typography 
                      variant="body2" 
                      fontWeight="bold"
                      color={health.jobs.recent_failures > 0 ? 'error' : 'text.primary'}
                    >
                      {health.jobs.recent_failures}
                    </Typography>
                  </Box>
                  <Box display="flex" justifyContent="space-between">
                    <Typography variant="body2">Held Test Faxes:</Typography>
                    <Typography variant="body2" fontWeight="bold">
                      {health.jobs.held ?? 'Unavailable'}
                    </Typography>
                  </Box>
                  <Box display="flex" justifyContent="space-between">
                    <Typography variant="body2">Needs review:</Typography>
                    <Typography variant="body2" fontWeight="bold"
                      color={(health.jobs.reconciliation_required ?? 0) > 0 ? warningTextColor : 'text.primary'}>
                      {health.jobs.reconciliation_required ?? 'Unavailable'}
                    </Typography>
                  </Box>
                </Box>
                {(health.jobs.reconciliation_required ?? 0) > 0 && (
                  <Typography variant="caption" color={warningTextColor} sx={{ display: 'block', mt: 1 }}>
                    Check your provider account before resending faxes that need review.
                  </Typography>
                )}
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
                onClick={() => onNavigate?.('inbox')}
              >
                <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                  <Typography variant="h6" component="h2" gutterBottom sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                    Inbound Fax
                  </Typography>
                  <Chip
                    label={health.inbound_enabled ? 'Enabled' : 'Disabled'}
                    color={health.inbound_enabled ? 'success' : 'warning'}
                    variant="outlined"
                    sx={{ color: health.inbound_enabled ? undefined : warningTextColor }}
                  />
                </CardContent>
              </Card>
            </Tooltip>
          </Grid>

          {/* Security Status */}
          <Grid item xs={12} sm={6} lg={3}>
            <Tooltip title="Click to manage API keys" arrow>
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
                onClick={() => onNavigate?.('keys')}
              >
                <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                  <Typography variant="h6" component="h2" gutterBottom sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                    Security
                  </Typography>
                  <Box display="flex" flexDirection="column" gap={1}>
                    <Box display="flex" alignItems="center">
                      <CheckCircleIcon color="success" />
                      <Typography variant="body2" sx={{ ml: 1 }}>
                        Authentication required
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

          {/* Spending by provider, last 30 days */}
          <Grid item xs={12} sm={6} lg={4}>
            <DeliveryCard title="Spending, last 30 days" hint="Click to view delivery routes" data={spending} onOpen={() => onNavigate?.('routes')}>
              {(providers) => providers.length === 0 ? (
                <Typography variant="body2" color="text.secondary">No faxes sent in the last 30 days.</Typography>
              ) : (
                <Box display="flex" flexDirection="column" gap={1}>
                  {providers.map((provider) => (
                    <Line key={provider.provider_id} label={provider.label} value={formatMoneyList(provider.estimated_cost, 'No price set')} />
                  ))}
                  {providers.length > 1 && <Line label="Total" value={totalCost(providers).map(formatMoney).join(' + ') || 'No price set'} />}
                </Box>
              )}
            </DeliveryCard>
          </Grid>

          {/* Received faxes waiting for or done with email delivery */}
          <Grid item xs={12} sm={6} lg={4}>
            <DeliveryCard title="Email delivery" hint="Click to view the inbox" data={intake} onOpen={() => onNavigate?.('inbox')}>
              {(counts) => (
                <Box display="flex" flexDirection="column" gap={1}>
                  <Line label="Waiting" value={counts.received + counts.sending} />
                  <Line label="Delivered" value={counts.delivered} />
                  <Line label="Not delivered" value={counts.failed} color={counts.failed > 0 ? 'error' : undefined} />
                </Box>
              )}
            </DeliveryCard>
          </Grid>

          {/* Direct delivery partners */}
          <Grid item xs={12} sm={6} lg={4}>
            <DeliveryCard title="Direct partners" hint="Click to view direct partners" data={partners} onOpen={() => onNavigate?.('routes')}>
              {(peers) => {
                const verified = peers.filter((peer) => peer.state === 'verified').length;
                const pending = peers.filter((peer) => peer.state === 'pending').length;
                return (
                  <Box display="flex" flexDirection="column" gap={1}>
                    <Line label="Verified" value={verified} />
                    <Line label="Waiting for verification" value={pending} color={pending > 0 ? warningTextColor : undefined} />
                  </Box>
                );
              }}
            </DeliveryCard>
          </Grid>

          {/* Config Overview */}
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>Config Overview</Typography>
                <Grid container spacing={1}>
                  <Grid item xs={6}><Typography variant="body2" color="text.secondary">Default provider</Typography></Grid>
                  <Grid item xs={6}><Chip size="small" label={cfg?.backend ?? 'Unavailable'} /></Grid>
                  <Grid item xs={6}><Typography variant="body2" color="text.secondary">Storage</Typography></Grid>
                  <Grid item xs={6}><Chip size="small" label={cfg?.storage?.backend || 'local'} /></Grid>
                  <Grid item xs={6}><Typography variant="body2" color="text.secondary">Authentication</Typography></Grid>
                  <Grid item xs={6}><Chip size="small" label="Required" color="success" variant="outlined" /></Grid>
                  <Grid item xs={6}><Typography variant="body2" color="text.secondary">Enforce HTTPS</Typography></Grid>
                  <Grid item xs={6}><Chip size="small" label={(cfg?.enforce_public_https ? 'Enabled' : 'Disabled')} color={cfg?.enforce_public_https ? 'success' : 'default'} variant="outlined" /></Grid>
                  <Grid item xs={6}><Typography variant="body2" color="text.secondary">v3 Plugins</Typography></Grid>
                  <Grid item xs={6}><Chip size="small" label={(cfg?.v3_plugins?.enabled ? `Enabled (${cfg?.v3_plugins?.active_outbound || '-'})` : 'Disabled')} color={cfg?.v3_plugins?.enabled ? 'success' : 'default'} variant="outlined" /></Grid>
                </Grid>
              </CardContent>
            </Card>
          </Grid>

          {/* MCP Overview */}
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>MCP Overview</Typography>
                <Grid container spacing={1} alignItems="center">
                  <Grid item xs={4}><Typography variant="body2" color="text.secondary">SSE</Typography></Grid>
                  <Grid item xs={8}>
                    <Chip size="small" label={cfg?.mcp?.sse_enabled ? 'Enabled' : 'Disabled'} color={cfg?.mcp?.sse_enabled ? 'success' : 'default'} variant="outlined" />
                    {cfg?.mcp?.sse_enabled && (
                      <Tooltip title="Copy SSE URL">
                        <IconButton size="small" sx={{ ml: 1 }} onClick={() => navigator.clipboard.writeText(`${window.location.origin}${cfg?.mcp?.sse_path || '/mcp/sse'}`)}>
                          <ContentCopyIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                  </Grid>
                  <Grid item xs={4}><Typography variant="body2" color="text.secondary">HTTP</Typography></Grid>
                  <Grid item xs={8}>
                    <Chip size="small" label={cfg?.mcp?.http_enabled ? 'Enabled' : 'Disabled'} color={cfg?.mcp?.http_enabled ? 'success' : 'default'} variant="outlined" />
                    {cfg?.mcp?.http_enabled && (
                      <Tooltip title="Copy HTTP URL">
                        <IconButton size="small" sx={{ ml: 1 }} onClick={() => navigator.clipboard.writeText(`${window.location.origin}${cfg?.mcp?.http_path || '/mcp/http'}`)}>
                          <ContentCopyIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                  </Grid>
                  <Grid item xs={4}><Typography variant="body2" color="text.secondary">OAuth</Typography></Grid>
                  <Grid item xs={8}><Chip size="small" label={cfg?.mcp?.require_oauth ? 'Required' : 'Optional'} variant="outlined" /></Grid>
                </Grid>
              </CardContent>
            </Card>
          </Grid>

          {/* Plugins (feature-gated) */}
          {cfg?.v3_plugins?.enabled && (
            <Grid item xs={12} md={6}>
              <Card>
                <CardContent>
                  <Typography variant="h6" gutterBottom>Plugins</Typography>
                  <Typography variant="body2" color="text.secondary">
                    Outbound: {cfg?.v3_plugins?.active_outbound || '-'} • Installed: {plugins?.length ?? 0}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    Manifest warnings appear under Diagnostics.
                  </Typography>
                </CardContent>
              </Card>
            </Grid>
          )}

          {/* SDK & Quickstart */}
          <Grid item xs={12} md={cfg?.v3_plugins?.enabled ? 6 : 12}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>SDK & Quickstart</Typography>
                <Typography variant="body2">Base URL: {window.location.origin}</Typography>
                <Typography variant="body2" sx={{ mb: 1 }}>Header: X-API-Key: &lt;your key&gt;</Typography>
                <Typography variant="body2" color="text.secondary">Node:</Typography>
                <Box component="pre" sx={{ p: 1, bgcolor: 'background.default', borderRadius: 1, overflow: 'auto' }}>{`npm i faxbot@1.0.2
node -e "(async()=>{const FaxbotClient=require('faxbot');const c=new FaxbotClient('${window.location.origin}','<key>');const r=await c.sendFax('+15551234567','/path/to/file.pdf');console.log(r)})()"`}</Box>
                <Typography variant="body2" color="text.secondary">Python:</Typography>
                <Box component="pre" sx={{ p: 1, bgcolor: 'background.default', borderRadius: 1, overflow: 'auto' }}>{`pip install faxbot==1.0.2
python - <<'PY'
from faxbot import FaxbotClient
c=FaxbotClient('${window.location.origin}','<key>')
print(c.send_fax('+15551234567','/path/to/file.pdf'))
PY`}</Box>
              </CardContent>
            </Card>
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
