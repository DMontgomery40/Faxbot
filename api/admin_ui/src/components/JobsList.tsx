import { useState, useEffect, useRef } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
  Button,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  CircularProgress,
  Alert,
  Grid,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  List,
  ListItem,
  ListItemText,
  Divider,
  TextField,
  Checkbox,
  FormControlLabel,
} from '@mui/material';
import { Refresh as RefreshIcon, Send as SendIcon } from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import { FaxCostItem, costAmount, useFaxCosts } from './delivery/FaxCost';
import { FaxRouteItems, HeldFaxes } from './ProviderRulesHeld';
import { rulesApiFor } from './ProviderRulesApi';
import { FaxTogetherItem, togetherLine } from './delivery/SendingTogether';
import { FaxCertaintyItem } from './work/SentCertainty';
import { SentContinuation } from './work/SentContinuation';
import { DigitalFaxOutcome } from './delivery/DigitalMessages';
import type { FaxJob, OperatorDelivery, DeliveryHistoryEvent } from '../api/types';
import type { DirectDeliveryRecord, FaxCost } from '../api/deliveryTypes';
import { providerLabel } from '../providerLabels';
import { formatServerTime } from '../api/time';
import type { AdminDestination } from '../navigation';
import { FaxRequestedByItem } from './delivery/Connectors';


interface JobsListProps {
  client: AdminAPIClient;
  // A fax to open in Job Details on arrival, such as the one Send just queued.
  openJobId?: string | null;
  onOpened?: () => void;
  // Opens Send a fax; absent for people who may not send.
  onSendFax?: () => void;
  // Holds the "Approve faxes" permission: may approve, refuse or send anyway the faxes rules held.
  canApprove?: boolean;
  onNavigate?: (destination: AdminDestination) => void;
}

const statusOptions = [
  { value: '', label: 'All' },
  { value: 'held', label: 'Held test fax' },
  { value: 'ready', label: 'Ready to send' },
  { value: 'preparing', label: 'Preparing' },
  { value: 'submitting', label: 'Sending' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'reconciliation_required', label: 'Needs review' },
  { value: 'success', label: 'Delivered' },
  { value: 'failed', label: 'Failed' },
  { value: 'cancelled', label: 'Cancelled' },
];

function deliveryState(job: FaxJob): string {
  return (job.delivery_state || job.status).toLowerCase();
}

function statusLabel(state: string): string {
  return statusOptions.find(option => option.value === state)?.label
    ?? state.replace(/_/g, ' ').replace(/^./, character => character.toUpperCase());
}

function deliveryNotice(job: FaxJob): string | null {
  if (deliveryState(job) === 'held' || job.dispatch_mode === 'held') {
    return 'This test fax is held and will not be sent.';
  }
  if (deliveryState(job) === 'reconciliation_required') {
    return job.dispatch_mode === 'legacy'
      ? 'Imported fax with no delivery record; check your provider account for the outcome.'
      : "Delivery couldn't be confirmed; check your provider account and confirm receipt instead of resending.";
  }
  return null;
}

const eventLabels: Record<string, string> = {
  accepted: 'Fax accepted',
  legacy_migrated: 'Imported from an earlier version',
  binding_unavailable: 'Original provider account unavailable',
  held_acceptance_restored: 'Held test fax restored',
  claimed: 'Preparing to send',
  dispatch_paused: 'Sending paused',
  submission_authorized: 'Sending to provider',
  submission_uncertain: 'Provider response unclear',
  sent_together: 'Going in one call with other faxes to this number',
  batch_split: 'Going on its own instead of with other faxes',
  preparation_failed: 'Could not prepare fax',
  preparation_expired: 'Preparation timed out',
  provider_observation_refused: 'Provider update ignored',
  terminal_conflict: 'Conflicting provider update ignored',
  late_observation: 'Late provider update',
  provider_observed: 'Provider status update',
  operator_identity_bound: 'Receipt confirmed with the provider fax ID',
  route_assigned: 'Route chosen',
  route_fallback: 'Trying the next route',
  repair_started: 'Sending only the missing pages directly to the partner',
  repair_completed: 'Completed directly by the partner after the call broke',
  repair_failed: 'The partner did not receive the missing pages',
};

const categoryLabels: Record<string, string> = {
  transport_ambiguous: 'Provider did not confirm the fax was accepted',
  response_unusable: 'Provider reply could not be read',
  submission_cancelled: 'Sending was interrupted',
  worker_lost: 'Sending stopped unexpectedly',
  artifact_unavailable: 'Document unavailable',
  provider_unavailable: 'Original provider unavailable',
  preparation_failed: 'Could not prepare fax',
  profile_mismatch: 'Provider account did not match',
  sid_mismatch: 'Provider fax ID did not match',
  provider_failed: 'The provider reported that the fax failed',
  partner_not_received: 'The direct delivery partner did not receive it',
  local_not_delivered: 'It could not go straight into Received, so Faxbot sent it by phone call',
  partly_sent: 'Part of this fax may have arrived before the call failed',
  pages_unconfirmed: 'The call ended without confirming which pages arrived',
};

// How a fax went by direct delivery, from the partner's answer. The direct
// record's message id is the delivery attempt id; a fax that fell back to
// fax after a refusal has the direct attempt among its earlier events.
type DirectOutcome = { text: string; severity: 'success' | 'info' | 'warning'; hideFaxId: boolean };

function directOutcome(delivery: OperatorDelivery | null, records: DirectDeliveryRecord[] | null,
  jobId: string | null = null): DirectOutcome | null {
  if (!delivery || !records) return null;
  const current = delivery.attempt?.id ?? null;
  const attempts = new Set([current, ...delivery.events.map((event) => event.attempt_id)].filter(Boolean));
  const sent = records.filter((record) => record.direction === 'outbound' && attempts.has(record.message_id));
  const forCurrent = sent.find((record) => record.message_id === current);
  const partner = (record: DirectDeliveryRecord) => record.partner || 'the partner';
  if (forCurrent?.state === 'accepted') {
    // A fax image went directly, with no telephone call; it is never called "faxed". Nor is an original that
    // went with a one-page notice fax: only the notice page was faxed.
    // Sent once to a partner's intake, or as a reference or the changes to a copy it held: the server's sentence.
    const text = forCurrent.send_once ? forCurrent.send_once : forCurrent.kind === 'fax_image'
      ? `Delivered directly as a fax image to ${partner(forCurrent)}; no telephone call.`
      : forCurrent.notice
        ? `Delivered directly to ${partner(forCurrent)}; only a one-page notice went by fax.`
        : `Delivered directly to ${partner(forCurrent)}.`;
    return { text, severity: 'success', hideFaxId: true };
  }
  // A call that broke part way, completed by sending only the missing pages directly to the partner.
  const repaired = records.find((record) => record.direction === 'outbound' && record.kind === 'repair'
    && record.state === 'accepted' && jobId !== null && record.job_id === jobId);
  if (repaired) return { text: repaired.status, severity: 'success', hideFaxId: false };
  if (forCurrent && (forCurrent.state === 'sending' || forCurrent.state === 'uncertain')) {
    return { text: "Waiting for the partner's answer.", severity: 'info', hideFaxId: true };
  }
  // A refusal means nothing reached the partner and the fax went by fax, which
  // may still need its provider fax ID.
  if (sent.some((record) => record.state === 'refused')) {
    return { text: 'Direct delivery refused, sent by fax instead.', severity: 'warning', hideFaxId: false };
  }
  return null;
}

function eventDetails(event: DeliveryHistoryEvent): string {
  const details = event.details;
  return [
    details.category && categoryLabels[details.category],
    details.reason,
    details.status && `Status: ${statusLabel(details.status)}`,
    details.dispatch_mode && `Sending mode: ${statusLabel(details.dispatch_mode)}`,
    // details.actor is an internal sign-in ID, so it is not shown.
    details.provider_sid && `Provider fax ID: ${details.provider_sid}`,
    details.route && `Route: ${details.route === 'direct' ? 'Direct delivery' : providerLabel(details.route)}`,
    details.legacy_status && `Earlier status: ${details.legacy_status}`,
  ].filter(Boolean).join(' • ');
}

interface DetailSelection { jobId: string }

export const URGENT_TEXT = 'Urgent: it goes before other faxes waiting for the same line.';
export const BY_CALL_TEXT = 'You asked for a real phone call through your carrier, even if the number is one of your own.';

function routeName(route: string): string {
  if (route === 'local') return 'This Faxbot';
  return route === 'direct' ? 'Direct delivery' : providerLabel(route);
}

// The route that carried the fax (its latest attempt), and any route tried before it.
export function routeText(backend: string, cost?: FaxCost | null): string {
  const routes = cost?.routes ?? [];
  if (!routes.length) return providerLabel(backend);
  const last = routeName(routes[routes.length - 1]);
  const earlier = routes.slice(0, -1).map(routeName);
  return earlier.length ? `${last} (after ${earlier.join(', ')})` : last;
}

function JobsList({ client, openJobId, onOpened, onSendFax, canApprove = false, onNavigate }: JobsListProps) {
  const [jobs, setJobs] = useState<FaxJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [total, setTotal] = useState(0);
  const [selectedJob, setSelectedJob] = useState<FaxJob | null>(null);
  const [jobDetailOpen, setJobDetailOpen] = useState(false);
  const [jobActionError, setJobActionError] = useState<string | null>(null);
  const [jobActionMessage, setJobActionMessage] = useState<string | null>(null);
  const [delivery, setDelivery] = useState<OperatorDelivery | null>(null);
  const [directRecords, setDirectRecords] = useState<DirectDeliveryRecord[] | null>(null);
  const [deliveryError, setDeliveryError] = useState<string | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [reviewRequired, setReviewRequired] = useState(true);
  const [providerIdDraft, setProviderIdDraft] = useState('');
  const [originalAccountConfirmed, setOriginalAccountConfirmed] = useState(false);
  const detailSelectionRef = useRef<DetailSelection | null>(null);
  const detailActionRef = useRef<object | null>(null);
  const costs = useFaxCosts(client, jobs);

  useEffect(() => {
    setJobDetailOpen(false);
    setSelectedJob(null);
    setDelivery(null);
    setDetailBusy(false);
    return () => {
      detailSelectionRef.current = null;
      detailActionRef.current = null;
    };
  }, [client]);

  const fetchJobs = async () => {
    try {
      setError(null);
      setLoading(true);
      const params = statusFilter ? { status: statusFilter } : {};
      const data = await client.listJobs(params);
      setJobs(data.jobs);
      setTotal(data.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sent faxes could not be loaded.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchJobs();
  }, [statusFilter, client]);

  useEffect(() => {
    // Auto-refresh jobs every 10 seconds
    const interval = setInterval(fetchJobs, 10000);
    return () => clearInterval(interval);
  }, [statusFilter, client]);

  const getStatusColor = (status: string): 'success' | 'error' | 'warning' | 'info' | 'default' => {
    switch (status.toLowerCase()) {
      case 'success':
      case 'completed':
        return 'success';
      case 'failed':
      case 'error':
        return 'error';
      case 'queued':
      case 'held':
      case 'ready':
      case 'preparing':
        return 'info';
      case 'submitting':
      case 'reconciliation_required':
      case 'in_progress':
      case 'sending':
        return 'warning';
      default:
        return 'default';
    }
  };

  // The shared server-time reading: local time, and a dash rather than a raw date it cannot read.
  const formatDate = (dateString: string) => formatServerTime(dateString);

  const beginDetailAction = (selection: DetailSelection): object | null => {
    if (detailSelectionRef.current !== selection || detailActionRef.current) return null;
    const action = {};
    detailActionRef.current = action;
    setDetailBusy(true);
    setJobActionError(null);
    setJobActionMessage(null);
    return action;
  };

  const finishDetailAction = (selection: DetailSelection, action: object) => {
    if (detailSelectionRef.current === selection && detailActionRef.current === action) {
      detailActionRef.current = null;
      setDetailBusy(false);
    }
  };

  const loadDetail = async (selection: DetailSelection): Promise<boolean> => {
    setOriginalAccountConfirmed(false);
    setReviewRequired(true);
    setDeliveryError(null);
    const [jobResult, deliveryResult, directResult] = await Promise.allSettled([
      client.getJob(selection.jobId), client.getDelivery(selection.jobId), client.listDirectDeliveries(),
    ]);
    if (detailSelectionRef.current !== selection) return false;
    // Direct delivery records need settings access; without them the dialog works as before.
    setDirectRecords(directResult.status === 'fulfilled' ? directResult.value.deliveries : null);
    if (jobResult.status === 'fulfilled') setSelectedJob(jobResult.value);
    else setJobActionError(jobResult.reason instanceof Error ? jobResult.reason.message : "Couldn't load this fax. Select Reload to try again.");
    if (deliveryResult.status === 'fulfilled') setDelivery(deliveryResult.value);
    else setDeliveryError(deliveryResult.reason instanceof Error ? deliveryResult.reason.message : "Couldn't load the delivery attempts. Select Reload to try again.");
    const newerJob = jobResult.status === 'fulfilled' && deliveryResult.status === 'fulfilled'
      && (jobResult.value.delivery_version ?? 0) > deliveryResult.value.version;
    if (newerJob) setDeliveryError('This fax changed while loading. Select Reload to see the latest.');
    const complete = jobResult.status === 'fulfilled' && deliveryResult.status === 'fulfilled' && !newerJob;
    setReviewRequired(!complete);
    return complete;
  };

  const handleJobClick = async (jobId: string) => {
    const selection = { jobId };
    detailSelectionRef.current = selection;
    detailActionRef.current = null;
    setSelectedJob(null);
    setDelivery(null);
    setDeliveryError(null);
    setProviderIdDraft('');
    setOriginalAccountConfirmed(false);
    setReviewRequired(true);
    setJobDetailOpen(true);
    const action = beginDetailAction(selection);
    if (!action) return;
    try { await loadDetail(selection); }
    finally { finishDetailAction(selection, action); }
  };

  // Arriving from Send's "Follow it in Sent" opens that fax's details.
  useEffect(() => {
    if (!openJobId) return;
    onOpened?.();
    void handleJobClick(openJobId);
  }, [openJobId]);

  const handleCloseJobDetail = () => {
    detailSelectionRef.current = null;
    detailActionRef.current = null;
    setDetailBusy(false);
    setJobDetailOpen(false);
    setSelectedJob(null);
    setJobActionError(null);
    setJobActionMessage(null);
    setDelivery(null);
    setDeliveryError(null);
    setProviderIdDraft('');
    setOriginalAccountConfirmed(false);
    setReviewRequired(true);
  };

  const handleReloadDelivery = async () => {
    const selection = detailSelectionRef.current;
    if (!selection) return;
    const action = beginDetailAction(selection);
    if (!action) return;
    try { await loadDetail(selection); }
    finally { finishDetailAction(selection, action); }
  };

  const handleRefreshStatus = async () => {
    const selection = detailSelectionRef.current;
    if (!selection) return;
    const action = beginDetailAction(selection);
    if (!action) return;
    setOriginalAccountConfirmed(false);
    setReviewRequired(true);
    try {
      await client.refreshJob(selection.jobId);
      if (detailSelectionRef.current !== selection) return;
      const complete = await loadDetail(selection);
      if (complete) setJobActionMessage('Status updated.');
    } catch (err) {
      if (detailSelectionRef.current === selection) {
        setJobActionError(err instanceof Error ? err.message : "Couldn't refresh the status. Try again.");
      }
    } finally { finishDetailAction(selection, action); }
  };

  const validProviderId = providerIdDraft.length >= 1 && providerIdDraft.length <= 100
    && !/[^A-Za-z0-9_-]/.test(providerIdDraft);

  const handleAttachProviderIdentity = async () => {
    const selection = detailSelectionRef.current;
    if (!selection || !canAttachFaxId || !delivery || reviewRequired
        || !selectedJob || !validProviderId || !originalAccountConfirmed) return;
    const action = beginDetailAction(selection);
    if (!action) return;
    const expectedVersion = delivery.version;
    setOriginalAccountConfirmed(false);
    setReviewRequired(true);
    let attached = false;
    try {
      const updated = await client.attachProviderIdentity(selection.jobId, {
        expected_version: expectedVersion, provider_sid: providerIdDraft,
        confirm_original_account: true,
      });
      if (detailSelectionRef.current !== selection) return;
      attached = true;
      setDelivery(updated);
      setDeliveryError(null);
      setProviderIdDraft('');
      setJobActionMessage('Receipt confirmed. Select Refresh status to check delivery with the provider.');
      const complete = await loadDetail(selection);
      if (detailSelectionRef.current === selection && !complete) {
        setJobActionError("Receipt confirmed, but the details couldn't be reloaded. Select Reload.");
      }
    } catch (err) {
      if (detailSelectionRef.current === selection) {
        setJobActionError(attached
          ? "Receipt confirmed, but the details couldn't be reloaded. Select Reload."
          : err instanceof Error ? err.message : "Couldn't tell whether the receipt was recorded. Select Reload to check.");
      }
    } finally { finishDetailAction(selection, action); }
  };

  const handleDownloadPdf = async () => {
    const selection = detailSelectionRef.current;
    if (!selection) return;
    const action = beginDetailAction(selection);
    if (!action) return;
    try {
      const blob = await client.downloadJobPdf(selection.jobId);
      if (detailSelectionRef.current !== selection) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `fax_${selection.jobId}.pdf`;
      document.body.appendChild(anchor);
      try { anchor.click(); }
      finally {
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 60000);
      }
    } catch (err) {
      if (detailSelectionRef.current === selection) {
        setJobActionError(err instanceof Error ? err.message : "Couldn't download the PDF.");
      }
    } finally { finishDetailAction(selection, action); }
  };

  const direct = directOutcome(delivery, directRecords, selectedJob?.id ?? null);
  const canAttachFaxId = Boolean(delivery?.can_bind_provider_identity) && !direct?.hideFaxId;

  const detailJob = selectedJob && delivery
    && delivery.version >= (selectedJob.delivery_version ?? 0) ? {
    ...selectedJob, delivery_state: delivery.state, dispatch_mode: delivery.dispatch_mode,
    delivery_version: delivery.version,
  } : selectedJob;

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems={{ xs: 'flex-start', sm: 'center' }}
        flexDirection={{ xs: 'column', sm: 'row' }} gap={2} mb={3}>
        <Box>
          <Typography variant="h4" component="h1">
            Sent
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Faxes sent from this installation, newest first. Select one to see its delivery attempts.
          </Typography>
        </Box>
      <HeldFaxes api={rulesApiFor(client)} canApprove={canApprove} onNavigate={onNavigate} />
        <Box display="flex" gap={1}>
          {onSendFax && (
            <Button variant="contained" startIcon={<SendIcon />} onClick={onSendFax}>
              Send a fax
            </Button>
          )}
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchJobs}
            disabled={loading}
          >
            Refresh
          </Button>
        </Box>
      </Box>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6} md={3}>
          <FormControl fullWidth>
            <InputLabel id="jobs-status-label" shrink>Show</InputLabel>
            <Select
              id="jobs-status-filter"
              labelId="jobs-status-label"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              label="Show"
              displayEmpty
              renderValue={(value) => statusOptions.find(option => option.value === value)?.label ?? value}
            >
              {statusOptions.map(option => (
                <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>
              ))}
            </Select>
          </FormControl>
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
            {total} {total === 1 ? 'fax' : 'faxes'}
          </Typography>
        </Grid>
      </Grid>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      <Card>
        <CardContent>
          {loading && jobs.length === 0 ? (
            <Box display="flex" justifyContent="center" py={4}>
              <CircularProgress />
            </Box>
          ) : jobs.length === 0 ? (
            <Box textAlign="center" py={4}>
              <Typography variant="body1" color="text.secondary">
                {statusFilter ? 'No faxes match this filter.' : 'No faxes sent yet.'}
              </Typography>
            </Box>
          ) : (
            <TableContainer sx={{ overflowX: 'auto' }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ minWidth: 100 }}>To</TableCell>
                    <TableCell sx={{ minWidth: 160 }}>Status</TableCell>
                    <TableCell sx={{ minWidth: 80, display: { xs: 'none', md: 'table-cell' } }}>Route</TableCell>
                    <TableCell sx={{ minWidth: 60, display: { xs: 'none', md: 'table-cell' } }}>Pages</TableCell>
                    <TableCell sx={{ minWidth: 90, display: { xs: 'none', md: 'table-cell' } }}>Cost</TableCell>
                    <TableCell sx={{ minWidth: 120 }}>Created</TableCell>
                    <TableCell sx={{ minWidth: 120, display: { xs: 'none', sm: 'table-cell' } }}>Updated</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {jobs.map((job) => (
                    <TableRow 
                      key={job.id} 
                      hover 
                      sx={{ cursor: 'pointer' }}
                      onClick={() => handleJobClick(job.id)}
                    >
                      <TableCell>
                        <Typography variant="body2" fontFamily="monospace" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {job.to_number}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Chip
                          label={statusLabel(deliveryState(job))}
                          color={getStatusColor(deliveryState(job))}
                          size="small"
                          variant="outlined"
                          sx={{ fontSize: { xs: '0.6rem', sm: '0.75rem' } }}
                        />
                        {deliveryNotice(job) && <Typography variant="caption" display="block" sx={{ mt: 0.5 }}>
                          {deliveryNotice(job)}
                        </Typography>}
                        {togetherLine(job.together) && <Typography variant="caption" display="block" sx={{ mt: 0.5 }}
                          data-testid="job-together">{togetherLine(job.together)}</Typography>}
                        {job.error && (
                          // The whole sentence, wrapped between words; never cut mid-word.
                          <Typography
                            variant="caption"
                            color="error"
                            data-testid="job-error"
                            sx={{
                              display: 'block',
                              mt: 0.5,
                              maxWidth: 320,
                              whiteSpace: 'normal',
                              overflowWrap: 'normal',
                              wordBreak: 'normal',
                              fontSize: { xs: '0.6rem', sm: '0.75rem' }
                            }}
                          >
                            {job.error}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {routeText(job.backend, costs.get(job.id))}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {job.pages || '-'}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" title={costs.get(job.id)?.summary ?? undefined} data-testid="job-cost"
                          sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {costAmount(costs.get(job.id))}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" color="text.secondary" sx={{ fontSize: { xs: '0.6rem', sm: '0.75rem' } }}>
                          {formatDate(job.created_at)}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                        <Typography variant="caption" color="text.secondary" sx={{ fontSize: { xs: '0.6rem', sm: '0.75rem' } }}>
                          {formatDate(job.updated_at)}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
          
          {jobs.length > 0 && (
            <Box mt={2}>
              <Typography variant="caption" color="text.secondary">
                Updates every 10 seconds.
              </Typography>
            </Box>
          )}
        </CardContent>
      </Card>

      {/* Job Detail Modal */}
      <Dialog open={jobDetailOpen} onClose={handleCloseJobDetail} maxWidth="md" fullWidth aria-labelledby="fax-job-details-title">
        <DialogTitle id="fax-job-details-title">
          Fax details
        </DialogTitle>
        <DialogContent>
          {detailBusy && <Box display="flex" alignItems="center" gap={1} sx={{ mb: 2 }} role="status">
            <CircularProgress size={20} aria-label="Job action in progress" />
            <Typography variant="body2">Working…</Typography>
          </Box>}
          {jobActionError && <Alert severity="error" sx={{ mb: 2 }}>{jobActionError}</Alert>}
          {jobActionMessage && <Alert severity="info" sx={{ mb: 2 }}>{jobActionMessage}</Alert>}
          {detailJob && deliveryNotice(detailJob) && <Alert severity="warning" sx={{ mb: 2 }}>
            {deliveryNotice(detailJob)}
          </Alert>}
          {detailJob && (
            <List>
              <ListItem>
                <ListItemText
                  primary="Fax ID"
                  secondary={detailJob.id}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="To"
                  secondary={detailJob.to_number}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Status"
                  secondary={
                    <Chip
                      label={statusLabel(deliveryState(detailJob))}
                      color={getStatusColor(deliveryState(detailJob))}
                      size="small"
                      variant="outlined"
                    />
                  }
                />
              </ListItem>
              {detailJob.dispatch_mode && <>
                <Divider />
                <ListItem><ListItemText primary="Sending Mode" secondary={statusLabel(detailJob.dispatch_mode)} /></ListItem>
              </>}
              {detailJob.provider_sid && <>
                <Divider />
                <ListItem><ListItemText primary="Provider Fax ID" secondary={detailJob.provider_sid} /></ListItem>
              </>}
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Route"
                  secondary={<>
                    {routeText(detailJob.backend, costs.get(detailJob.id))}
                    {/* Why Faxbot chose it, as recorded when it chose it. */}
                    {detailJob.waiting_reason && (
                      <Typography component="span" variant="body2" color="text.secondary" display="block"
                        data-testid="job-waiting-reason">
                        {detailJob.waiting_reason}
                      </Typography>
                    )}
                    {/* The send-by time, and whether the fax may miss it. */}
                    {detailJob.send_by && (
                      <Typography component="span" variant="body2" display="block"
                        color={detailJob.send_by.at_risk ? 'warning.main' : 'text.secondary'}
                        data-testid="job-send-by">
                        {detailJob.send_by.sentence}
                      </Typography>
                    )}
                    {detailJob.urgent && (
                      <Typography component="span" variant="body2" color="text.secondary" display="block"
                        data-testid="job-urgent">
                        {URGENT_TEXT}
                      </Typography>
                    )}
                    {detailJob.send_by_call && (
                      <Typography component="span" variant="body2" color="text.secondary" display="block"
                        data-testid="job-by-call">
                        {BY_CALL_TEXT}
                      </Typography>
                    )}
                    {costs.get(detailJob.id)?.route_explanation && (
                      <Typography component="span" variant="body2" color="text.secondary" display="block"
                        data-testid="job-route-reason">
                        {costs.get(detailJob.id)?.route_explanation}
                      </Typography>
                    )}
                    {costs.get(detailJob.id)?.dialed?.sentence && (
                      <Typography component="span" variant="body2" color="text.secondary" display="block"
                        data-testid="job-dialed-number">
                        {costs.get(detailJob.id)?.dialed?.sentence}
                      </Typography>
                    )}
                    {costs.get(detailJob.id)?.recipient_warning?.sentence && (
                      <Typography component="span" variant="body2" color="warning.main" display="block"
                        data-testid="job-recipient-warning">
                        {costs.get(detailJob.id)?.recipient_warning?.sentence}
                      </Typography>
                    )}
                  </>}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Pages"
                  secondary={detailJob.pages || 'Unknown'}
                />
              </ListItem>
              {detailJob.fax_engine?.sentence && (
                <ListItem data-testid="job-fax-engine">
                  <ListItemText primary="How the pages went" secondary={detailJob.fax_engine.sentence} />
                </ListItem>
              )}
              {detailJob.page_layout?.sentences?.length ? (
                <ListItem data-testid="job-pages">
                  <ListItemText primary="How the pages were sent" secondary={detailJob.page_layout.sentences.join(' ')} />
                </ListItem>
              ) : null}
              {detailJob.coding?.sentence && (
                <ListItem data-testid="job-coding">
                  <ListItemText primary="Fax coding" secondary={<>
                    <Typography component="span" variant="body2" color="text.secondary" display="block">
                      {detailJob.coding.sentence}
                    </Typography>
                    {detailJob.coding.measured_sentence && (
                      <Typography component="span" variant="caption" color="text.secondary" display="block">
                        {detailJob.coding.measured_sentence}
                      </Typography>
                    )}
                  </>} />
                </ListItem>
              )}
              {detailJob.fax_engine?.negotiation?.sentence && (
                <ListItem data-testid="job-call-negotiation">
                  <ListItemText primary="How the call went" secondary={detailJob.fax_engine.negotiation.sentence} />
                </ListItem>
              )}
              {detailJob.fax_engine?.changes?.length ? (
                <ListItem data-testid="job-call-changes">
                  <ListItemText primary="Changed for this call" secondary={detailJob.fax_engine.changes.join(' ')} />
                </ListItem>
              ) : null}
              <FaxCostItem client={client} jobId={detailJob.id} />
              <FaxRouteItems api={rulesApiFor(client)} jobId={detailJob.id} />
              <FaxRequestedByItem client={client} jobId={detailJob.id} />
              <FaxTogetherItem client={client} jobId={detailJob.id} together={detailJob.together} onChanged={() => void fetchJobs()} />
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Document"
                  secondary={detailJob.file_name || 'Unknown'}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Created"
                  secondary={formatDate(detailJob.created_at)}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Updated"
                  secondary={formatDate(detailJob.updated_at)}
                />
              </ListItem>
              {detailJob.error && (
                <>
                  <Divider />
                  <ListItem>
                    <ListItemText
                      primary="What went wrong"
                      secondary={
                        <Alert severity="error" sx={{ mt: 1 }}>
                          <Typography variant="body2">
                            {detailJob.error}
                          </Typography>
                        </Alert>
                      }
                    />
                  </ListItem>
                </>
              )}
            </List>
          )}
          {/* A fax Faxbot could not confirm: its owner, the checks cheapest first, and settling it. */}
          {detailJob && <FaxCertaintyItem client={client} jobId={detailJob.id} onOpenFax={(faxId) => void handleJobClick(faxId)} />}
          {/* A fax whose call broke part way: send only its remaining pages, and the link both ways. */}
          {detailJob && <SentContinuation client={client} jobId={detailJob.id} onOpenFax={(faxId) => void handleJobClick(faxId)} />}
          <Divider sx={{ my: 2 }} />
          <Typography variant="h6" component="h2" gutterBottom>Delivery attempts</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Each try to send this fax and what the provider reported, as Faxbot recorded it.
          </Typography>
          {deliveryError && <Alert severity="error" sx={{ mb: 2 }}>{deliveryError}</Alert>}
          {reviewRequired && !detailBusy && !deliveryError && !jobActionError && <Alert severity="warning" sx={{ mb: 2 }}>
            Select Reload to see the latest details before confirming receipt.
          </Alert>}
          {selectedJob && <DigitalFaxOutcome client={client} jobId={selectedJob.id} />}
          {delivery && <>
            {direct && <Alert severity={direct.severity} sx={{ mb: 2 }}>{direct.text}</Alert>}
            <List dense>
              {/* The account is kept by ID in the API; there is no name to show for it here. */}
              <ListItem><ListItemText primary="Provider of the latest attempt"
                secondary={delivery.provider_id ? providerLabel(delivery.provider_id) : 'Unavailable'} /></ListItem>
            </List>
            <Typography variant="subtitle1" component="h3" sx={{ mt: 2 }}>Latest attempt</Typography>
            {delivery.attempt ? <List dense>
              <ListItem><ListItemText primary="Stage" secondary={statusLabel(delivery.attempt.phase)} /></ListItem>
              <ListItem><ListItemText primary="Provider fax ID" secondary={delivery.attempt.provider_sid ?? 'None yet'} /></ListItem>
              <ListItem><ListItemText primary="Submitted" secondary={delivery.attempt.submitted_at ? formatDate(delivery.attempt.submitted_at) : 'Not yet'} /></ListItem>
              <ListItem><ListItemText primary="Completed" secondary={delivery.attempt.completed_at ? formatDate(delivery.attempt.completed_at) : 'Not yet'} /></ListItem>
            </List> : <Typography variant="body2" color="text.secondary" sx={{ my: 1 }}>No send attempts yet.</Typography>}
            {!delivery.can_bind_provider_identity && !direct?.hideFaxId && delivery.state === 'reconciliation_required' && <Typography
              variant="body2" color="text.secondary" sx={{ my: 2 }}>
              Receipt can't be confirmed for this fax from here.
            </Typography>}
            {canAttachFaxId && selectedJob && <Box component="form"
              onSubmit={(event) => { event.preventDefault(); void handleAttachProviderIdentity(); }} sx={{ my: 2 }}>
              <Typography variant="subtitle1" component="h3" gutterBottom>Confirm receipt</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                If your provider's account shows it accepted this fax, enter the fax ID shown there so Faxbot can follow it. This never sends the fax again.
              </Typography>
              <TextField fullWidth label="Provider fax ID" value={providerIdDraft}
                onChange={(event) => {
                  if (detailActionRef.current) return;
                  setProviderIdDraft(event.target.value);
                  setOriginalAccountConfirmed(false);
                }}
                disabled={detailBusy}
                error={providerIdDraft.length > 0 && !validProviderId}
                helperText="Up to 100 letters, numbers, hyphens or underscores."
              />
              <FormControlLabel sx={{ my: 1 }} control={<Checkbox
                checked={originalAccountConfirmed}
                disabled={detailBusy || reviewRequired}
                onChange={(event) => {
                  if (!detailActionRef.current && !reviewRequired) setOriginalAccountConfirmed(event.target.checked);
                }}
              />} label="I checked: this fax ID is in the original provider's account and matches this fax's number, document and time." />
              <Button type="submit" variant="contained"
                disabled={detailBusy || reviewRequired || !validProviderId || !originalAccountConfirmed}>
                Confirm receipt
              </Button>
            </Box>}
            <Typography variant="subtitle1" component="h3" sx={{ mt: 2 }}>What happened</Typography>
            {delivery.events_truncated && <Typography variant="caption" color="text.secondary" display="block" sx={{ my: 1 }}>
              Showing the latest 100 events.
            </Typography>}
            {delivery.events.length === 0 ? <Typography variant="body2" color="text.secondary" sx={{ my: 1 }}>
              No delivery events yet.
            </Typography> : <List dense>
              {delivery.events.map((event) => <ListItem key={event.id} alignItems="flex-start">
                <ListItemText primary={eventLabels[event.kind] ?? 'Delivery update'}
                  secondaryTypographyProps={{ component: 'div' }} secondary={<>
                    <Typography variant="body2" color="text.secondary">{formatDate(event.created_at)}</Typography>
                    {eventDetails(event) && <Typography variant="body2">{eventDetails(event)}</Typography>}
                  </>} />
              </ListItem>)}
            </List>}
          </>}
        </DialogContent>
        <DialogActions>
          {selectedJob && <Button onClick={handleDownloadPdf} disabled={detailBusy}>Download PDF</Button>}
          {selectedJob && <Button onClick={handleRefreshStatus} disabled={detailBusy}>Refresh status</Button>}
          <Button onClick={handleReloadDelivery} disabled={detailBusy}>Reload</Button>
          <Button onClick={handleCloseJobDetail}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

export default JobsList;
