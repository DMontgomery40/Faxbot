// Overview: what Faxbot is doing for this organization, the next worthwhile improvements, everyday faxes, what
// needs attention, and the map of every way Faxbot saves money. Each block reads its own sources and says its own
// state and time; a serious problem moves Needs attention to the top while it lasts.
import { useState, useEffect, type ReactNode } from 'react';
import { AnalysisCard } from './AIAnalysis';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Grid,
  Chip,
  Button,
  CircularProgress,
  Tooltip,
  useTheme,
} from '@mui/material';
import { darken } from '@mui/material/styles';
import {
  Refresh as RefreshIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  AutoAwesome as CapabilitiesIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { HealthStatus } from '../api/types';
import type { DirectPartner, SavingsMechanisms, Savings, SendingRecommendations } from '../api/deliveryTypes';
import type { Capabilities } from '../api/capabilityTypes';
import type { FactAdvice } from '../api/factAdviceTypes';
import SavingsMap from './SavingsMap';
import type { SipCallRecord } from '../api/sipTypes';
import type { AdminDestination } from '../navigation';
import { spendingLines, spendingTotalText } from './delivery/spendingSummary';
import { providerLabel } from '../providerLabels';
import NeedsAttention from './overview/NeedsAttention';
import {
  attentionView, loadAttentionSources, NOT_READY_TEXT, notReadyFor, settle, type AttentionSources, type Loaded,
} from './overview/attention';
import {
  blockState, clockText, connectNext, everydayLines, nextImprovements, resultLines, STALE_AFTER_MS, staleText,
  usedCapabilities,
} from './overview/blocks';
import { BlockFrame, DoingContent, EverydayContent, NextContent } from './overview/OverviewBlocks';

type CardData<T> = Loaded<T>;

const DAY_MS = 24 * 60 * 60 * 1000;
// How often the page looks again at its own times, to say when a block has gone stale.
const TICK_MS = 30 * 1000;

// A received trunk call that left no fax image never reaches the Inbox; the
// newest one from the last day is named on the inbound card instead.
export function missedInboundCall(calls: SipCallRecord[], now: number = Date.now()): string | null {
  const missed = calls.find((call) => call.direction === 'inbound' && call.job_id === null && call.summary
    && now - new Date(call.started_at).getTime() < DAY_MS);
  return missed?.summary ?? null;
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

const cardTitleSx = { fontSize: { xs: '1rem', sm: '1.125rem' } };

// A card for one delivery area. It opens its screen only when its data is available to this account.
function DeliveryCard<T>({ title, hint, data, onOpen, children }: {
  title: string;
  hint: string;
  data: CardData<T>;
  onOpen?: () => void;
  children: (value: T) => ReactNode;
}) {
  const ready = data.kind === 'ready';
  const body = (
    <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
      <Typography variant="h6" component="h3" gutterBottom sx={cardTitleSx}>{title}</Typography>
      {data.kind === 'loading' ? <CircularProgress size={20} aria-label={`Loading ${title}`} />
        : data.kind === 'ready' ? children(data.data)
        : <Typography variant="body2" color="text.secondary">{CARD_TEXT[data.kind]}</Typography>}
    </CardContent>
  );
  if (!ready || !onOpen) return <Card variant="outlined" sx={{ height: '100%' }}>{body}</Card>;
  return (
    <Tooltip title={hint} arrow>
      <Card variant="outlined" sx={clickableCardSx} onClick={onOpen} role="button" aria-label={title} tabIndex={0}
        onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpen(); } }}>
        {body}
      </Card>
    </Tooltip>
  );
}

// A card that opens a page, from the mouse or the keyboard.
function OpenCard({ hint, onOpen, children }: { hint: string; onOpen: () => void; children: ReactNode }) {
  return (
    <Tooltip title={hint} arrow>
      <Card variant="outlined" sx={clickableCardSx} onClick={onOpen} tabIndex={0}
        onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpen(); } }}>
        {children}
      </Card>
    </Tooltip>
  );
}

function Line({ label, value, color }: { label: string; value: ReactNode; color?: string }) {
  return (
    <Box display="flex" justifyContent="space-between" gap={2}>
      <Typography variant="body2">{label}</Typography>
      <Typography variant="body2" fontWeight="bold" color={color ?? 'text.primary'} sx={{ textAlign: 'right' }}>{value}</Typography>
    </Box>
  );
}

// An install that only receives: no sending provider, one that receives. Its status is about receiving.
function receivesOnly(health: HealthStatus): boolean {
  return !health.backend && !!health.receiving_backend;
}

// Ready for what the install is set up for: sending, receiving, or both (the rule `faxbot system health`
// uses); notReadyFor names the direction that is not ready now.
function statusReady(health: HealthStatus): boolean {
  // No provider in either direction is never ready; "No fax provider set up yet" says why.
  return (!!health.backend || !!health.receiving_backend) && notReadyFor(health) === null;
}

interface DashboardProps {
  client: AdminAPIClient;
  onNavigate?: (destination: AdminDestination) => void;
  // May this account open the Setup Wizard (settings:write)?
  canSetUp?: boolean;
  canReadAnalysis?: boolean;
  // Opens Send a fax; absent for people who may not send.
  onSendFax?: () => void;
  // May this account read settings? Only then are what Faxbot did, the next improvements and the map read.
  canReadSettings?: boolean;
  // The time now, in milliseconds; tests pass their own.
  clock?: () => number;
}

type ValueSources = {
  capabilities: Loaded<Capabilities>;
  savings: Loaded<Savings>;
  sending: Loaded<SendingRecommendations>;
  facts: Loaded<FactAdvice>;
};

const LOADING = { kind: 'loading' } as const;

function Dashboard({ client, onNavigate, canSetUp = false, canReadAnalysis = false, onSendFax, canReadSettings = false,
  clock = Date.now }: DashboardProps) {
  const theme = useTheme();
  const warningTextColor = theme.palette.mode === 'light'
    ? darken(theme.palette.warning.light, 0.6)
    : theme.palette.warning.main;
  const [healthState, setHealthState] = useState<Loaded<HealthStatus>>(LOADING);
  const [cfg, setCfg] = useState<any | null>(null);
  const [sources, setSources] = useState<Omit<AttentionSources, 'health'>>({
    holds: LOADING, work: LOADING, intake: LOADING, expected: LOADING, costs: LOADING, network: LOADING,
  });
  const [value, setValue] = useState<ValueSources>({ capabilities: LOADING, savings: LOADING, sending: LOADING, facts: LOADING });
  const [partners, setPartners] = useState<CardData<DirectPartner[]>>(LOADING);
  const [missedCall, setMissedCall] = useState<string | null>(null);
  const [mechanisms, setMechanisms] = useState<CardData<SavingsMechanisms>>(LOADING);
  const [now, setNow] = useState(clock);
  const health = healthState.kind === 'ready' ? healthState.data : null;
  const loading = healthState.kind === 'loading';
  const healthError = healthState.kind === 'denied' ? 'Server health is not available to this account.'
    : healthState.kind === 'error' || healthState.kind === 'unavailable' ? 'Could not load server health. Select Refresh to try again.'
      : null;

  // What Faxbot did and the next improvements need settings:read; without it they are never asked for.
  const fetchValue = async () => {
    if (!canReadSettings) {
      const denied = { kind: 'denied', at: clock() } as const;
      setValue({ capabilities: denied, savings: denied, sending: denied, facts: denied });
      return;
    }
    void settle(client.getSavingsMechanisms(), clock).then(setMechanisms);
    const [capabilities, savings, sending, facts] = await Promise.all([
      settle(client.getCapabilities(), clock),
      settle(client.getSavings(), clock),
      settle(client.getSendingRecommendations(), clock),
      settle(client.call<FactAdvice>({ method: 'GET', path: '/routing/recommendations/facts' }), clock),
    ]);
    setValue({ capabilities, savings, sending, facts });
  };

  // Needs attention and the everyday cards load on entry and on Refresh, not on every health poll.
  const fetchDelivery = async () => {
    const [attention, peers, calls] = await Promise.all([
      loadAttentionSources(client, clock),
      settle(client.listDirectPartners().then((result) => result.peers), clock),
      settle(client.listSipCalls({ limit: 20, direction: 'inbound' }).then((result) => result.items), clock),
    ]);
    setSources(attention);
    setPartners(peers);
    setMissedCall(calls.kind === 'ready' ? missedInboundCall(calls.data, clock()) : null);
    setNow(clock());
  };

  const fetchHealth = async () => {
    const next = await settle(client.getHealthStatus(), clock);
    // A failed check keeps what the last poll showed; its time then says how old it is.
    setHealthState((current) => (next.kind === 'ready' || current.kind !== 'ready' ? next : current));
  };

  const fetchConfig = async () => {
    try {
      setCfg(await client.getConfig());
    } catch {
      // The status card says "Unavailable" for what it could not read.
    }
  };

  const refresh = () => { void fetchHealth(); void fetchDelivery(); void fetchValue(); };

  useEffect(() => {
    refresh();
    // Faxbot's status is polled; only its own time changes with each poll.
    const cleanup = client.startPolling((data) => {
      setHealthState({ kind: 'ready', data, at: clock() });
      setNow(clock());
      fetchConfig();
    });
    const tick = window.setInterval(() => setNow(clock()), TICK_MS);
    return () => { cleanup(); window.clearInterval(tick); };
  }, [client]);

  const getStatusColor = (healthy: boolean) => (healthy ? 'success' : 'error');
  const getStatusIcon = (healthy: boolean) => (healthy ? <CheckCircleIcon /> : <ErrorIcon />);

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" minHeight={200}>
        <CircularProgress />
      </Box>
    );
  }

  const attention = attentionView({ health: healthState, ...sources }, { canSetUp });
  const newInstall = !!health && !health.backend && !health.receiving_backend;
  const capabilities = value.capabilities.kind === 'ready' ? value.capabilities.data : null;
  const days = capabilities?.days ?? 30;

  // 1. What Faxbot is doing: what acted on faxes here, and the results in their own units.
  const savingsFailed = value.savings.kind === 'error' || value.savings.kind === 'unavailable';
  const used = capabilities ? usedCapabilities(capabilities) : [];
  const results = value.savings.kind === 'ready' ? resultLines(value.savings.data) : null;
  const doing = blockState(savingsFailed ? [value.capabilities] : [value.capabilities, value.savings],
    { now, empty: !newInstall && used.length === 0 && results !== null && results.length === 0 });
  // A new installation's one Set up a fax provider button is here when this block can show it, else on the status card.
  const doingOffersSetUp = newInstall && canSetUp && !!onNavigate && (doing.state === 'ready' || doing.state === 'stale');
  const doingBlock = (
    <BlockFrame id="doing" title="What Faxbot is doing" state={doing.state} at={doing.at}>
      <DoingContent used={used} results={results} days={days}
        newInstall={newInstall} connect={capabilities ? connectNext(capabilities) : []} canSetUp={canSetUp}
        onNavigate={onNavigate} />
    </BlockFrame>
  );

  // 2. Next improvements: capabilities ready to turn on, then the top advice; each says what kind of step it is.
  const adviceFailed = [value.sending, value.facts].filter((source) => source.kind === 'error' || source.kind === 'unavailable');
  const improvements = nextImprovements({
    capabilities, sending: value.sending.kind === 'ready' ? value.sending.data : null,
    facts: value.facts.kind === 'ready' ? value.facts.data : null,
  });
  const next = blockState([value.capabilities, ...[value.sending, value.facts].filter((source) => !adviceFailed.includes(source))],
    { now, empty: improvements.length === 0 });
  const nextBlock = (
    <BlockFrame id="next" title="Next improvements" state={next.state} at={next.at}
      action={canReadSettings && onNavigate ? (
        <Button size="small" startIcon={<CapabilitiesIcon />} onClick={() => onNavigate('savings/capabilities')}>
          Every capability
        </Button>
      ) : undefined}>
      <NextContent items={improvements} onNavigate={onNavigate} />
      {adviceFailed.length > 0 && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }} data-testid="overview-next-partial">
          Some advice could not be checked. Select Refresh to try again.
        </Typography>
      )}
    </BlockFrame>
  );

  // 3. Everyday faxes: Send, and the Received, Sent and Expected counts, with the status and delivery cards.
  const counts = [sources.work, sources.expected, sources.intake];
  const countsLoading = counts.some((source) => source.kind === 'loading');
  const countTimes = counts.flatMap((source) => (source.kind === 'ready' ? [source.at] : []));
  const countsAt = countTimes.length ? Math.min(...countTimes) : null;
  const everydayState = countsLoading ? 'loading' : countsAt !== null && now - countsAt > STALE_AFTER_MS ? 'stale' : 'ready';
  const statusAt = healthState.kind === 'ready' ? healthState.at : null;
  const statusStale = statusAt !== null && now - statusAt > STALE_AFTER_MS;
  const everydayBlock = (
    <BlockFrame id="everyday" title="Everyday faxes" state={everydayState} at={countsAt}>
      <EverydayContent lines={everydayLines({ health: healthState, work: sources.work, expected: sources.expected, intake: sources.intake })}
        onSendFax={onSendFax} onNavigate={onNavigate}>
        {healthError && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }} data-testid="overview-health-error">{healthError}</Typography>
        )}
        <Grid container spacing={2}>
          {health && (
            <>
              {/* Status, and what sends and receives faxes */}
              <Grid item xs={12} sm={6} lg={4}>
                <OpenCard hint="Open health and diagnostics" onOpen={() => onNavigate?.('admin/health')}>
                  <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                    <Box display="flex" alignItems="center" mb={{ xs: 1, sm: 2 }}>
                      {getStatusIcon(statusReady(health))}
                      <Typography variant="h6" component="h3" sx={{ ml: 1, ...cardTitleSx }}>System Status</Typography>
                    </Box>
                    <Chip label={statusReady(health) ? 'Ready' : 'Needs attention'} color={getStatusColor(statusReady(health))}
                      variant="outlined" />
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
                    {!health.backend && !receivesOnly(health) && canSetUp && onNavigate && !doingOffersSetUp && (
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
                    {statusAt !== null && (
                      <Typography variant="caption" color={statusStale ? 'warning.main' : 'text.secondary'} sx={{ display: 'block', mt: 1 }}
                        data-testid="overview-status-checked">
                        {statusStale ? staleText(statusAt) : `Status checked ${clockText(statusAt)}.`}
                      </Typography>
                    )}
                  </CardContent>
                </OpenCard>
              </Grid>

              {/* Sending now */}
              <Grid item xs={12} sm={6} lg={4}>
                <OpenCard hint="Open Sent" onOpen={() => onNavigate?.('jobs')}>
                  <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                    <Typography variant="h6" component="h3" gutterBottom sx={cardTitleSx}>Outbound Delivery</Typography>
                    <Box display="flex" flexDirection="column" gap={1}>
                      <Line label="Ready / Preparing:" value={health.jobs.queued} />
                      {(health.jobs.waiting_for_line ?? 0) > 0 && (
                        <Box display="flex" justifyContent="space-between" data-testid="waiting-for-line">
                          <Typography variant="body2">Waiting for a free line:</Typography>
                          <Typography variant="body2" fontWeight="bold">{health.jobs.waiting_for_line}</Typography>
                        </Box>
                      )}
                      <Line label="Submitting / In Progress:" value={health.jobs.in_progress} />
                      <Line label="Failures (Last 24 Hours):" value={health.jobs.recent_failures}
                        color={health.jobs.recent_failures > 0 ? 'error' : undefined} />
                      <Line label="Needs review:" value={health.jobs.reconciliation_required ?? 'Unavailable'}
                        color={(health.jobs.reconciliation_required ?? 0) > 0 ? warningTextColor : undefined} />
                    </Box>
                    {(health.jobs.reconciliation_required ?? 0) > 0 && (
                      <Typography variant="caption" color={warningTextColor} sx={{ display: 'block', mt: 1 }}>
                        Check your provider account before resending faxes that need review.
                      </Typography>
                    )}
                  </CardContent>
                </OpenCard>
              </Grid>

              {/* Receiving */}
              <Grid item xs={12} sm={6} lg={4}>
                <OpenCard hint="Open Received" onOpen={() => onNavigate?.('inbox')}>
                  <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
                    <Typography variant="h6" component="h3" gutterBottom sx={cardTitleSx}>Inbound Fax</Typography>
                    <Chip label={health.inbound_enabled ? 'Enabled' : 'Disabled'} color={health.inbound_enabled ? 'success' : 'warning'}
                      variant="outlined" sx={{ color: health.inbound_enabled ? undefined : warningTextColor }} />
                    {missedCall && (
                      <Typography variant="body2" sx={{ mt: 1, color: warningTextColor }} data-testid="missed-inbound-call">
                        {missedCall}
                      </Typography>
                    )}
                  </CardContent>
                </OpenCard>
              </Grid>
            </>
          )}

          {/* Spending by provider, last 30 days */}
          <Grid item xs={12} sm={6} lg={4}>
            <DeliveryCard title="Spending, last 30 days" hint="Click to view spending" data={sources.costs} onOpen={() => onNavigate?.('routes')}>
              {(costs) => {
                // The same reading of spending as Spending (spendingSummary).
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
            <DeliveryCard title="Email delivery" hint="Click to view received faxes" data={sources.intake} onOpen={() => onNavigate?.('inbox')}>
              {(intakeCounts) => (
                <Box display="flex" flexDirection="column" gap={1}>
                  <Line label="Waiting" value={intakeCounts.received + intakeCounts.sending} />
                  <Line label="Delivered" value={intakeCounts.delivered} />
                  <Line label="Not delivered" value={intakeCounts.failed} color={intakeCounts.failed > 0 ? 'error' : undefined} />
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
        </Grid>
      </EverydayContent>
    </BlockFrame>
  );

  // 4. Needs attention; first while a serious problem lasts.
  const attentionBlock = <NeedsAttention view={attention} onNavigate={onNavigate} now={now} />;
  const blocks: Array<[string, ReactNode]> = [['doing', doingBlock], ['next', nextBlock], ['everyday', everydayBlock],
    ['attention', attentionBlock]];
  const ordered = attention.serious ? [blocks[3], ...blocks.slice(0, 3)] : blocks;

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" gap={1} flexWrap="wrap" mb={3}>
        <Typography variant="h4" component="h1">
          Overview
        </Typography>
        <Box display="flex" gap={1} flexWrap="wrap">
          {canReadSettings && onNavigate && (
            <Button variant="contained" startIcon={<CapabilitiesIcon />} onClick={() => onNavigate('savings/capabilities')}>
              Capabilities
            </Button>
          )}
          <Button variant="outlined" startIcon={<RefreshIcon />} onClick={refresh} disabled={loading}>
            Refresh
          </Button>
        </Box>
      </Box>

      {ordered.map(([key, node]) => <Box key={key} data-block={key}>{node}</Box>)}

      {canReadAnalysis && <AnalysisCard client={client} onNavigate={onNavigate} />}

      {/* 5. Every way Faxbot saves money, along a fax's path (owner decision 2026-10-08: at the bottom). Nothing
          shows for a person who may not read settings. */}
      {canReadSettings && mechanisms.kind === 'ready' && (
        <Box data-block="map">
          <SavingsMap data={mechanisms.data} capabilities={capabilities} onNavigate={onNavigate} />
          <Typography variant="caption" color={now - mechanisms.at > STALE_AFTER_MS ? 'warning.main' : 'text.secondary'}
            sx={{ display: 'block', mt: 1 }} data-testid="overview-map-checked">
            {now - mechanisms.at > STALE_AFTER_MS ? staleText(mechanisms.at) : `Map checked ${clockText(mechanisms.at)}.`}
          </Typography>
        </Box>
      )}
      {canReadSettings && mechanisms.kind === 'error' && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 3 }} data-testid="savings-map-error">
          The map of how Faxbot saves money could not load. Select Refresh to try again.
        </Typography>
      )}
    </Box>
  );
}

export default Dashboard;
