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
import { Refresh as RefreshIcon } from '@mui/icons-material';
import AdminAPIClient, { reconciliationNotice } from '../api/client';
import type { FaxJob, OperatorDelivery, DeliveryHistoryEvent } from '../api/types';

interface JobsListProps {
  client: AdminAPIClient;
}

const statusOptions = [
  { value: '', label: 'All Statuses' },
  { value: 'held', label: 'Held (test)' },
  { value: 'ready', label: 'Ready' },
  { value: 'preparing', label: 'Preparing' },
  { value: 'submitting', label: 'Submitting' },
  { value: 'in_progress', label: 'In Progress' },
  { value: 'reconciliation_required', label: 'Reconciliation Required' },
  { value: 'success', label: 'Success' },
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
    return 'This test fax will never be automatically transmitted, even after outbound sending is enabled.';
  }
  if (deliveryState(job) === 'reconciliation_required') {
    return reconciliationNotice(job.reconciliation_reason);
  }
  return null;
}

const eventLabels: Record<string, string> = {
  accepted: 'Fax accepted',
  legacy_migrated: 'Historical fax imported',
  binding_unavailable: 'Original account binding unavailable',
  held_acceptance_restored: 'Held test acceptance restored',
  claimed: 'Preparation claimed by worker',
  dispatch_paused: 'Sending paused before submission',
  submission_authorized: 'External submission authorized',
  submission_uncertain: 'Submission outcome uncertain',
  preparation_failed: 'Preparation failed before submission',
  preparation_expired: 'Preparation ownership expired',
  provider_observation_refused: 'Provider update refused',
  terminal_conflict: 'Conflicting final update recorded without changing outcome',
  late_observation: 'Late provider update recorded',
  provider_observed: 'Provider status recorded',
  operator_identity_bound: 'Provider fax ID attached by operator',
};

const categoryLabels: Record<string, string> = {
  transport_ambiguous: 'Provider response did not confirm acceptance',
  response_unusable: 'Provider reply could not be used',
  submission_cancelled: 'Submission was interrupted',
  worker_lost: 'Worker ownership expired',
  artifact_unavailable: 'Document unavailable',
  provider_unavailable: 'Captured provider unavailable',
  preparation_failed: 'Preparation failed',
  profile_mismatch: 'Original account did not match',
  sid_mismatch: 'Provider fax ID did not match',
};

function eventDetails(event: DeliveryHistoryEvent): string {
  const details = event.details;
  return [
    details.category && categoryLabels[details.category],
    details.status && `Status: ${statusLabel(details.status)}`,
    details.dispatch_mode && `Dispatch: ${statusLabel(details.dispatch_mode)}`,
    details.actor && `Operator: ${details.actor}`,
    details.provider_sid && `Provider fax ID: ${details.provider_sid}`,
    details.legacy_status && `Historical status: ${details.legacy_status}`,
  ].filter(Boolean).join(' • ');
}

interface DetailSelection { jobId: string }

function JobsList({ client }: JobsListProps) {
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
  const [deliveryError, setDeliveryError] = useState<string | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [reviewRequired, setReviewRequired] = useState(true);
  const [providerIdDraft, setProviderIdDraft] = useState('');
  const [originalAccountConfirmed, setOriginalAccountConfirmed] = useState(false);
  const detailSelectionRef = useRef<DetailSelection | null>(null);
  const detailActionRef = useRef<object | null>(null);

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
      setError(err instanceof Error ? err.message : 'Failed to fetch jobs');
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

  const formatDate = (dateString: string) => {
    try {
      const naiveUTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$/.test(dateString);
      const date = new Date(naiveUTC ? `${dateString}Z` : dateString);
      if (Number.isNaN(date.getTime())
          || (naiveUTC && date.toISOString().slice(0, 19) !== dateString.slice(0, 19))) {
        return dateString;
      }
      return date.toLocaleString();
    } catch {
      return dateString;
    }
  };

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
    const [jobResult, deliveryResult] = await Promise.allSettled([
      client.getJob(selection.jobId), client.getDelivery(selection.jobId),
    ]);
    if (detailSelectionRef.current !== selection) return false;
    if (jobResult.status === 'fulfilled') setSelectedJob(jobResult.value);
    else setJobActionError(jobResult.reason instanceof Error ? jobResult.reason.message : 'Job details could not be loaded. Reload delivery to try again.');
    if (deliveryResult.status === 'fulfilled') setDelivery(deliveryResult.value);
    else setDeliveryError(deliveryResult.reason instanceof Error ? deliveryResult.reason.message : 'Delivery history could not be loaded. Reload delivery to try again.');
    const newerJob = jobResult.status === 'fulfilled' && deliveryResult.status === 'fulfilled'
      && (jobResult.value.delivery_version ?? 0) > deliveryResult.value.version;
    if (newerJob) setDeliveryError('Delivery changed while loading. Reload delivery and review the updated history before continuing.');
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
      if (complete) setJobActionMessage('Status refresh completed. Job details and delivery history were reloaded.');
    } catch (err) {
      if (detailSelectionRef.current === selection) {
        setJobActionError(err instanceof Error ? err.message : 'Status refresh failed. Reload delivery before continuing.');
      }
    } finally { finishDetailAction(selection, action); }
  };

  const validProviderId = providerIdDraft.length >= 1 && providerIdDraft.length <= 100
    && !/[^A-Za-z0-9_-]/.test(providerIdDraft);

  const handleAttachProviderIdentity = async () => {
    const selection = detailSelectionRef.current;
    if (!selection || !delivery?.can_bind_provider_identity || reviewRequired
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
      setJobActionMessage('Provider fax ID attached. This did not resend the fax or mark it delivered. Refresh Status queries the original account.');
      const complete = await loadDetail(selection);
      if (detailSelectionRef.current === selection && !complete) {
        setJobActionError('Provider fax ID was attached, but current details or history could not be reloaded. Reload delivery to review the current record.');
      }
    } catch (err) {
      if (detailSelectionRef.current === selection) {
        setJobActionError(attached
          ? 'Provider fax ID was attached, but job details could not be reloaded. Reload delivery to review the current record.'
          : err instanceof Error ? err.message : 'Provider identity attachment was not confirmed. Reload delivery before continuing.');
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
        setJobActionError(err instanceof Error ? err.message : 'Failed to download PDF');
      }
    } finally { finishDetailAction(selection, action); }
  };

  const detailJob = selectedJob && delivery
    && delivery.version >= (selectedJob.delivery_version ?? 0) ? {
    ...selectedJob, delivery_state: delivery.state, dispatch_mode: delivery.dispatch_mode,
    delivery_version: delivery.version,
  } : selectedJob;

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          Fax Jobs
        </Typography>
        <Button
          variant="outlined"
          startIcon={<RefreshIcon />}
          onClick={fetchJobs}
          disabled={loading}
        >
          Refresh
        </Button>
      </Box>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6} md={3}>
          <FormControl fullWidth>
            <InputLabel id="jobs-status-label" shrink>Status Filter</InputLabel>
            <Select
              id="jobs-status-filter"
              labelId="jobs-status-label"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              label="Status Filter"
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
            Total: {total} jobs
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
                No jobs found
              </Typography>
            </Box>
          ) : (
            <TableContainer sx={{ overflowX: 'auto' }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ minWidth: 120 }}>Job ID</TableCell>
                    <TableCell sx={{ minWidth: 100, display: { xs: 'none', sm: 'table-cell' } }}>To Number</TableCell>
                    <TableCell sx={{ minWidth: 80 }}>Status</TableCell>
                    <TableCell sx={{ minWidth: 80, display: { xs: 'none', md: 'table-cell' } }}>Backend</TableCell>
                    <TableCell sx={{ minWidth: 60, display: { xs: 'none', md: 'table-cell' } }}>Pages</TableCell>
                    <TableCell sx={{ minWidth: 150, display: { xs: 'none', lg: 'table-cell' } }}>Error</TableCell>
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
                          {job.id.slice(0, 8)}...
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
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
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {job.backend}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {job.pages || '-'}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ maxWidth: 200, display: { xs: 'none', lg: 'table-cell' } }}>
                        {job.error && (
                          <Typography
                            variant="caption"
                            color="error"
                            sx={{
                              display: 'block',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                              fontSize: { xs: '0.6rem', sm: '0.75rem' }
                            }}
                            title={job.error}
                          >
                            {job.error}
                          </Typography>
                        )}
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
                Auto-refreshing every 10 seconds • Phone numbers are masked
              </Typography>
            </Box>
          )}
        </CardContent>
      </Card>

      {/* Job Detail Modal */}
      <Dialog open={jobDetailOpen} onClose={handleCloseJobDetail} maxWidth="md" fullWidth aria-labelledby="fax-job-details-title">
        <DialogTitle id="fax-job-details-title">
          Job Details
        </DialogTitle>
        <DialogContent>
          {detailBusy && <Box display="flex" alignItems="center" gap={1} sx={{ mb: 2 }} role="status">
            <CircularProgress size={20} aria-label="Job action in progress" />
            <Typography variant="body2">Job action in progress…</Typography>
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
                  primary="Job ID"
                  secondary={detailJob.id}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="To Number"
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
                <ListItem><ListItemText primary="Dispatch Mode" secondary={statusLabel(detailJob.dispatch_mode)} /></ListItem>
              </>}
              {detailJob.delivery_version != null && <>
                <Divider />
                <ListItem><ListItemText primary="Delivery Version" secondary={detailJob.delivery_version} /></ListItem>
              </>}
              {detailJob.provider_sid && <>
                <Divider />
                <ListItem><ListItemText primary="Provider Fax ID" secondary={detailJob.provider_sid} /></ListItem>
              </>}
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Backend"
                  secondary={detailJob.backend}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Pages"
                  secondary={detailJob.pages || 'Unknown'}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="File Name"
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
                  primary="Last Updated"
                  secondary={formatDate(detailJob.updated_at)}
                />
              </ListItem>
              {detailJob.error && (
                <>
                  <Divider />
                  <ListItem>
                    <ListItemText
                      primary="Error Details"
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
          <Divider sx={{ my: 2 }} />
          <Typography variant="h6" component="h2" gutterBottom>Original Account and Delivery History</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            These identifiers belong to the account captured when this fax was accepted, even if current Settings have changed.
            Times use your browser's local timezone.
          </Typography>
          {deliveryError && <Alert severity="error" sx={{ mb: 2 }}>{deliveryError}</Alert>}
          {reviewRequired && !detailBusy && <Alert severity="warning" sx={{ mb: 2 }}>
            Reload delivery, review the current account, attempt and history, then confirm again before attaching a fax ID.
            Your entered fax ID is retained until you close this job.
          </Alert>}
          {delivery && <>
            <List dense>
              <ListItem><ListItemText primary="Original Provider" secondary={delivery.provider_id ?? 'Unavailable'} /></ListItem>
              <ListItem><ListItemText primary="Original Account Profile" secondary={delivery.profile_id ?? 'Unavailable'} /></ListItem>
              <ListItem><ListItemText primary="Accepted Configuration Revision" secondary={delivery.revision_id ?? 'Unavailable'} /></ListItem>
            </List>
            <Typography variant="subtitle1" component="h3" sx={{ mt: 2 }}>Current Attempt</Typography>
            {delivery.attempt ? <List dense>
              <ListItem><ListItemText primary="Attempt ID" secondary={delivery.attempt.id} /></ListItem>
              <ListItem><ListItemText primary="Attempt Phase" secondary={statusLabel(delivery.attempt.phase)} /></ListItem>
              <ListItem><ListItemText primary="Provider Fax ID (Attempt)" secondary={delivery.attempt.provider_sid ?? 'No displayable provider identity recorded'} /></ListItem>
              <ListItem><ListItemText primary="Submitted" secondary={delivery.attempt.submitted_at ? formatDate(delivery.attempt.submitted_at) : 'No submission recorded'} /></ListItem>
              <ListItem><ListItemText primary="Completed" secondary={delivery.attempt.completed_at ? formatDate(delivery.attempt.completed_at) : 'No completion recorded'} /></ListItem>
            </List> : <Typography variant="body2" color="text.secondary" sx={{ my: 1 }}>No attempt is recorded.</Typography>}
            {!delivery.can_bind_provider_identity && delivery.bind_refusal_reason && <Alert
              severity={delivery.state === 'reconciliation_required' ? 'warning' : 'info'} sx={{ my: 2 }}>
              Identity attachment unavailable: {delivery.bind_refusal_reason}
            </Alert>}
            {delivery.can_bind_provider_identity && selectedJob && <Box component="form"
              onSubmit={(event) => { event.preventDefault(); void handleAttachProviderIdentity(); }} sx={{ my: 2 }}>
              <Typography variant="subtitle1" component="h3" gutterBottom>Attach Confirmed Provider Fax ID</Typography>
              <Alert severity="warning" sx={{ mb: 2 }}>
                Attaching an ID records matched evidence. It does not resend the fax or assert delivery.
                Status can then be recovered by polling the original account.
              </Alert>
              <TextField fullWidth label="Confirmed provider fax ID" value={providerIdDraft}
                onChange={(event) => {
                  if (detailActionRef.current) return;
                  setProviderIdDraft(event.target.value);
                  setOriginalAccountConfirmed(false);
                }}
                disabled={detailBusy}
                error={providerIdDraft.length > 0 && !validProviderId}
                helperText="Use 1–100 ASCII letters, digits, hyphens or underscores."
              />
              <FormControlLabel sx={{ my: 1 }} control={<Checkbox
                checked={originalAccountConfirmed}
                disabled={detailBusy || reviewRequired}
                onChange={(event) => {
                  if (!detailActionRef.current && !reviewRequired) setOriginalAccountConfirmed(event.target.checked);
                }}
              />} label="I matched this fax ID in the ORIGINAL provider account shown above against this fax's destination, document and submission time." />
              <Button type="submit" variant="contained"
                disabled={detailBusy || reviewRequired || !validProviderId || !originalAccountConfirmed}>
                Attach Confirmed Provider Fax ID
              </Button>
            </Box>}
            <Typography variant="subtitle1" component="h3" sx={{ mt: 2 }}>Delivery Events</Typography>
            {delivery.events_truncated && <Alert severity="info" sx={{ my: 1 }}>
              Showing the newest 100 events in chronological order; earlier events are omitted.
            </Alert>}
            {delivery.events.length === 0 ? <Typography variant="body2" color="text.secondary" sx={{ my: 1 }}>
              No delivery events are recorded.
            </Typography> : <List dense>
              {delivery.events.map((event) => <ListItem key={event.id} alignItems="flex-start">
                <ListItemText primary={eventLabels[event.kind] ?? 'Recorded delivery event'}
                  secondaryTypographyProps={{ component: 'div' }} secondary={<>
                    <Typography variant="body2" color="text.secondary">{formatDate(event.created_at)}</Typography>
                    {event.attempt_id && <Typography variant="caption" display="block">Attempt: {event.attempt_id}</Typography>}
                    {eventDetails(event) && <Typography variant="body2">{eventDetails(event)}</Typography>}
                  </>} />
              </ListItem>)}
            </List>}
          </>}
        </DialogContent>
        <DialogActions>
          {selectedJob && <Button onClick={handleDownloadPdf} disabled={detailBusy}>Download PDF</Button>}
          {selectedJob && <Button onClick={handleRefreshStatus} disabled={detailBusy}>Refresh Status</Button>}
          <Button onClick={handleReloadDelivery} disabled={detailBusy}>Reload Delivery</Button>
          <Button onClick={handleCloseJobDetail}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

export default JobsList;
