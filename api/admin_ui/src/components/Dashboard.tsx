import { useState, useEffect } from 'react';
import { WaitingForYouCard } from './ProviderRulesHeld';
import { rulesApiFor } from './ProviderRulesApi';
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
  ChevronRight as ChevronRightIcon,
  Send as SendIcon,
} from '@mui/icons-material';
import AdminAPIClient, { AdminAPIError, isNotAvailable } from '../api/client';
import type { HealthStatus, WorkCounts } from '../api/types';
import type { DirectPartner, IntakeCounts, RouteCostsResponse } from '../api/deliveryTypes';
import type { SipCallRecord } from '../api/sipTypes';
import type { SipNetworkReport } from '../api/networkTypes';
import type { AdminDestination } from '../navigation';
import { spendingLines, spendingTotalText } from './delivery/spendingSummary';
import { providerLabel } from '../providerLabels';
import { formatServerTime } from '../api/time';

type CardData<T> = { kind: 'loading' } | { kind: 'ready'; data: T } | { kind: 'denied' | 'unavailable' | 'error' };

const DAY_MS = 24 * 60 * 60 * 1000;

// A received trunk call that left no fax image never reaches the Inbox; the
// newest one from the last day is named on the inbound card instead.
export function missedInboundCall(calls: SipCallRecord[], now: number = Date.now()): string | null {
  const missed = calls.find((call) => call.direction === 'inbound' && call.job_id === null && call.summary
    && now - new Date(call.started_at).getTime() < DAY_MS);
  return missed?.summary ?? null;
}

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

// One thing that needs a person, with how many and the page that handles it.
export interface AttentionItem {
  key: string;
  label: string;
  count: number | null;
  destination: AdminDestination;
}

// An install that only receives: no sending provider, one that receives. Its status is about receiving.
function receivesOnly(health: HealthStatus): boolean {
  return !health.backend && !!health.receiving_backend;
}

// Ready for what the install is set up for: sending, receiving, or both (the rule `faxbot system health`
// uses). Which direction that is set up is not ready now, or null.
export function notReadyFor(health: HealthStatus): 'send' | 'receive' | 'both' | null {
  const sending = !!health.backend && !health.backend_healthy;
  const receiving = !!health.receiving_backend && !health.receiving_ready;
  return sending && receiving ? 'both' : sending ? 'send' : receiving ? 'receive' : null;
}

const NOT_READY_TEXT = {
  send: 'Faxbot is not ready to send faxes',
  receive: 'Faxbot is not ready to receive faxes',
  both: 'Faxbot is not ready to send or receive faxes',
} as const;

function statusReady(health: HealthStatus): boolean {
  // No provider in either direction is never ready; "No fax provider set up yet" says why.
  return (!!health.backend || !!health.receiving_backend) && notReadyFor(health) === null;
}

// What needs a person now, from the cards' own data. Items this account
// cannot read, and items with nothing in them, are left out.
export function attentionItems({ health, work, intake, costs, network, canSetUp = false }: {
  health: HealthStatus | null;
  work: CardData<WorkCounts>;
  intake: CardData<IntakeCounts>;
  costs: CardData<RouteCostsResponse>;
  // The carrier trunk's network check: an item while Faxbot keeps T.38 off because of the network.
  network?: CardData<SipNetworkReport>;
  canSetUp?: boolean;
}): AttentionItem[] {
  const items: AttentionItem[] = [];
  if (health && !health.backend && !health.receiving_backend) {
    items.push({ key: 'no-provider', label: 'No fax provider is set up yet', count: null,
      destination: canSetUp ? 'setup' : 'diagnostics' });
  }
  const notReady = health ? notReadyFor(health) : null;
  if (notReady) {
    items.push({ key: 'not-ready', label: NOT_READY_TEXT[notReady], count: null, destination: 'system/diagnostics' });
  }
  if (health?.jobs.recent_failures) {
    items.push({ key: 'failed', label: 'Faxes that failed in the last 24 hours', count: health.jobs.recent_failures, destination: 'faxes/sent' });
  }
  if (health?.jobs.reconciliation_required) {
    items.push({ key: 'uncertain', label: 'Sent faxes with an uncertain result', count: health.jobs.reconciliation_required, destination: 'faxes/sent' });
  }
  if (work.kind === 'ready' && work.data.unassigned > 0) {
    items.push({ key: 'unassigned', label: 'Received faxes waiting for an owner', count: work.data.unassigned, destination: 'faxes/received?show=waiting' });
  }
  if (work.kind === 'ready' && work.data.overdue > 0) {
    items.push({ key: 'overdue', label: 'Received faxes that are overdue', count: work.data.overdue, destination: 'faxes/received?show=overdue' });
  }
  if (intake.kind === 'ready' && intake.data.failed > 0) {
    items.push({ key: 'not-delivered', label: 'Received faxes not delivered by email', count: intake.data.failed, destination: 'faxes/received?show=not-delivered' });
  }
  if (costs.kind === 'ready') {
    const unrecorded = [...costs.data.providers, ...(costs.data.received ?? [])]
      .reduce((total, row) => total + (row.unrecorded_calls ?? 0), 0);
    if (unrecorded > 0) {
      items.push({ key: 'unrecorded', label: 'Carrier charges with no matching fax, last 30 days', count: unrecorded, destination: 'costs/spending' });
    }
  }
  if (network?.kind === 'ready' && network.data.applies && network.data.action === 'turned_off'
    && !network.data.t38_enabled) {
    items.push({ key: 't38-network', label: 'One network change would let faxes go over the internet; faxes still go through meanwhile',
      count: null,
      destination: 'providers/trunk' });
  }
  return items;
}

interface DashboardProps {
  client: AdminAPIClient;
  onNavigate?: (destination: AdminDestination) => void;
  // May this account open the Setup Wizard (settings:write)?
  canSetUp?: boolean;
  // Opens Send a fax; absent for people who may not send.
  onSendFax?: () => void;
}

function Dashboard({ client, onNavigate, canSetUp = false, onSendFax }: DashboardProps) {
  const theme = useTheme();
  const warningTextColor = theme.palette.mode === 'light'
    ? darken(theme.palette.warning.light, 0.6)
    : theme.palette.warning.main;
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cfg, setCfg] = useState<any | null>(null);
  const [spending, setSpending] = useState<CardData<RouteCostsResponse>>({ kind: 'loading' });
  const [intake, setIntake] = useState<CardData<IntakeCounts>>({ kind: 'loading' });
  const [partners, setPartners] = useState<CardData<DirectPartner[]>>({ kind: 'loading' });
  const [work, setWork] = useState<CardData<WorkCounts>>({ kind: 'loading' });
  const [network, setNetwork] = useState<CardData<SipNetworkReport>>({ kind: 'loading' });
  const [missedCall, setMissedCall] = useState<string | null>(null);

  // Delivery cards load on entry and on Refresh, not on every health poll.
  const fetchDelivery = async () => {
    const [costs, queue, peers, calls, counts, check] = await Promise.all([
      settle(client.getRouteCosts()),
      settle(client.listIntakeItems({ limit: 1 }).then((result) => result.counts)),
      settle(client.listDirectPartners().then((result) => result.peers)),
      settle(client.listSipCalls({ limit: 20, direction: 'inbound' }).then((result) => result.items)),
      settle(client.workCounts()),
      settle(client.getSipNetwork()),
    ]);
    setSpending(costs);
    setIntake(queue);
    setPartners(peers);
    setWork(counts);
    setNetwork(check);
    setMissedCall(calls.kind === 'ready' ? missedInboundCall(calls.data) : null);
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
      setCfg(await client.getConfig());
    } catch {
      // The status card says "Unavailable" for what it could not read.
    }
  };

  useEffect(() => {
    fetchHealth();
    void fetchDelivery();

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

  const attention = attentionItems({ health, work, intake, costs: spending, network, canSetUp });
  const attentionLoading = work.kind === 'loading' || intake.kind === 'loading' || spending.kind === 'loading';

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          Overview
        </Typography>
        <Box display="flex" gap={1}>
          {onSendFax && (
            <Button variant="contained" startIcon={<SendIcon />} onClick={onSendFax}>
              Send a fax
            </Button>
          )}
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={() => { void fetchHealth(); void fetchDelivery(); }}
            disabled={loading}
          >
            Refresh
          </Button>
        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      <WaitingForYouCard api={rulesApiFor(client)} onOpen={() => onNavigate?.('faxes/sent')} />

      {health && (
        <Grid container spacing={{ xs: 2, md: 3 }}>
          {/* System Status, and what sends and receives faxes */}
          <Grid item xs={12} sm={6} lg={3}>
            <Tooltip title="Click to view detailed diagnostics" arrow>
              <Card
                sx={{
                  cursor: 'pointer',
                  height: '100%',
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
                  {getStatusIcon(statusReady(health))}
                  <Typography variant="h6" component="h2" sx={{ ml: 1, fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                    System Status
                  </Typography>
                </Box>
                <Chip
                  label={statusReady(health) ? 'Ready' : 'Needs attention'}
                  color={getStatusColor(statusReady(health))}
                  variant="outlined"
                />
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  {health.backend ? `Sending: ${providerLabel(health.backend)}`
                    : receivesOnly(health) ? `Receiving: ${providerLabel(health.receiving_backend ?? '')}`
                      : 'No fax provider set up yet.'}
                </Typography>
                {notReadyFor(health) && (
                  <Typography variant="body2" sx={{ mt: 1 }} data-testid="status-not-ready">
                    {NOT_READY_TEXT[notReadyFor(health)!]}.
                  </Typography>
                )}
                {!health.backend && !receivesOnly(health) && canSetUp && onNavigate && (
                  <Button variant="contained" size="small" sx={{ mt: 1.5 }}
                    onClick={(event) => { event.stopPropagation(); onNavigate('setup'); }}>
                    Set up a fax provider
                  </Button>
                )}
                {(health.backend || receivesOnly(health)) && health.backend_message && (
                  <Typography variant="body2" color="error" sx={{ mt: 1 }} data-testid="engine-message">
                    {health.backend_message}
                  </Typography>
                )}
                <Box display="flex" flexDirection="column" gap={0.5} sx={{ mt: 1.5 }}>
                  <Box display="flex" justifyContent="space-between" alignItems="center" gap={1}>
                    <Typography variant="body2" color="text.secondary">Sending</Typography>
                    <Chip size="small" data-testid="config-sending"
                      label={cfg ? (cfg.hybrid?.outbound ? providerLabel(cfg.hybrid.outbound) : 'Not set up yet') : 'Unavailable'} />
                  </Box>
                  <Box display="flex" justifyContent="space-between" alignItems="center" gap={1}>
                    <Typography variant="body2" color="text.secondary">Receiving</Typography>
                    <Chip size="small" data-testid="config-receiving"
                      label={cfg ? (cfg.inbound?.enabled && cfg.hybrid?.inbound ? providerLabel(cfg.hybrid.inbound) : 'Not set up yet') : 'Unavailable'} />
                  </Box>
                </Box>
              </CardContent>
              </Card>
            </Tooltip>
          </Grid>

          {/* Needs attention: what is waiting for a person, each opening the page that handles it */}
          <Grid item xs={12} sm={6} lg={3}>
            <Card sx={{ height: '100%' }} data-testid="needs-attention">
              <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                <Typography variant="h6" component="h2" gutterBottom sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
                  Needs attention
                </Typography>
                {attention.length === 0 ? (
                  attentionLoading ? <CircularProgress size={20} aria-label="Loading Needs attention" />
                    : <Typography variant="body2" color="text.secondary">Nothing needs attention.</Typography>
                ) : (
                  <Box display="flex" flexDirection="column" gap={0.5}>
                    {attention.map((item) => (
                      <Button key={item.key} data-testid={`attention-${item.key}`} color="inherit" size="small"
                        disabled={!onNavigate} onClick={() => onNavigate?.(item.destination)}
                        endIcon={<ChevronRightIcon fontSize="small" />}
                        sx={{ justifyContent: 'space-between', textAlign: 'left', textTransform: 'none', px: 1, mx: -1 }}>
                        <Typography variant="body2" component="span" sx={{ flex: 1 }}>{item.label}</Typography>
                        {item.count !== null && (
                          <Typography variant="body2" component="span" fontWeight="bold" color={warningTextColor} sx={{ ml: 2 }}>
                            {item.count}
                          </Typography>
                        )}
                      </Button>
                    ))}
                  </Box>
                )}
              </CardContent>
            </Card>
          </Grid>

          {/* Job Queue */}
          <Grid item xs={12} sm={6} lg={3}>
            <Tooltip title="Click to view all jobs" arrow>
              <Card
                sx={{
                  cursor: 'pointer',
                  height: '100%',
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
                  {(health.jobs.waiting_for_line ?? 0) > 0 && (
                    <Box display="flex" justifyContent="space-between" data-testid="waiting-for-line">
                      <Typography variant="body2">Waiting for a free line:</Typography>
                      <Typography variant="body2" fontWeight="bold">{health.jobs.waiting_for_line}</Typography>
                    </Box>
                  )}
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
                  height: '100%',
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
                  {missedCall && (
                    <Typography variant="body2" sx={{ mt: 1, color: warningTextColor }} data-testid="missed-inbound-call">
                      {missedCall}
                    </Typography>
                  )}
                </CardContent>
              </Card>
            </Tooltip>
          </Grid>

          {/* Spending by provider, last 30 days */}
          <Grid item xs={12} sm={6} lg={4}>
            <DeliveryCard title="Spending, last 30 days" hint="Click to view spending" data={spending} onOpen={() => onNavigate?.('routes')}>
              {(costs) => {
                // The same reading of spending as Costs → Spending (spendingSummary).
                const lines = spendingLines(costs);
                return lines.length === 0 ? (
                  <Typography variant="body2" color="text.secondary">No faxes sent or received in the last 30 days.</Typography>
                ) : (
                  <Box display="flex" flexDirection="column" gap={1}>
                    {lines.map((line) => <Line key={line.key} label={line.label} value={line.value} />)}
                    {lines.length > 1 && <Line label="Total" value={spendingTotalText(costs)} />}
                  </Box>
                );
              }}
            </DeliveryCard>
          </Grid>

          {/* Received faxes waiting for or done with email delivery */}
          <Grid item xs={12} sm={6} lg={4}>
            <DeliveryCard title="Email delivery" hint="Click to view received faxes" data={intake} onOpen={() => onNavigate?.('inbox')}>
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
            <DeliveryCard title="Direct partners" hint="Click to view partners" data={partners} onOpen={() => onNavigate?.('recipients/partners')}>
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

          {/* Last Updated */}
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography variant="body2" color="text.secondary">
                  Last updated: {formatServerTime(health.timestamp)}
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
