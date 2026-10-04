import { useState, useEffect, useCallback } from 'react';
import {
  Box,
  Typography,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Button,
  CircularProgress,
  Alert,
  Chip,
  Paper,
  Card,
  CardContent,
  Stack,
  useTheme,
  useMediaQuery,
  Fade,
  Grow,
  IconButton,
  Tooltip,
  Snackbar,
} from '@mui/material';
import { 
  Refresh as RefreshIcon, 
  Download as DownloadIcon, 
  ContentCopy as ContentCopyIcon, 
  PlayArrow as TestIcon,
  Inbox as InboxIcon,
  Phone as PhoneIcon,
  Description as DocumentIcon,
  CalendarToday as DateIcon,
  CheckCircle as SuccessIcon,
  Error as ErrorIcon,
  Info as InfoIcon,
  Sync as FetchIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import { docsLink } from '../docsLinks';
import type { InboundFax } from '../api/types';
import type { EmailConnector, IntakeItem } from '../api/deliveryTypes';
import { parseServerTime } from '../api/time';
import { DeliveryStatusLine, DirectDeliveries, emailDeliveryApplies, inboundFaxStatus, isNewFax, providerName } from './delivery/InboxDelivery';
import type { DeliveryTone } from './delivery/InboxDelivery';
import { DeliveryError, Notice } from './delivery/shared';
import type { AdminDestination } from '../navigation';
import { ResponsiveFormSection } from './common/ResponsiveFormFields';

interface InboundProps {
  client: AdminAPIClient;
  docsBase?: string;
  inboundEnabled?: boolean;
  onNavigate?: (destination: AdminDestination) => void;
  // Installation permissions from /auth/me. Provider setup and test faxes are
  // shown only to people allowed to use them; the server checks again.
  permissions?: ReadonlySet<string>;
}

function Inbound({ client, docsBase, inboundEnabled, onNavigate, permissions }: InboundProps) {
  const canReadProviders = !!permissions?.has('providers:read');
  const canAddTestFax = !!permissions?.has('providers:write');
  // Email delivery status comes from the installation's intake queue; accounts
  // that cannot read it see the Inbox without it.
  const canReadDelivery = !!permissions?.has('mailboxes:read');
  const canRetryDelivery = !!permissions?.has('settings:write');
  const canOpenEmailSettings = !!permissions?.has('settings:read') && !!onNavigate;
  const [deliveries, setDeliveries] = useState<IntakeItem[] | null>(null);
  // Email connectors decide whether a fax's number has email delivery now.
  const [connectors, setConnectors] = useState<EmailConnector[] | null>(null);
  const [fetchingId, setFetchingId] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [deliveryError, setDeliveryError] = useState<unknown>(null);
  const [deliveryNotice, setDeliveryNotice] = useState<string | null>(null);
  const [faxes, setFaxes] = useState<InboundFax[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [callbacks, setCallbacks] = useState<any | null>(null);
  const [callbacksError, setCallbacksError] = useState<string | null>(null);
  const [simulating, setSimulating] = useState(false);
  const [copySnackbar, setCopySnackbar] = useState<string>('');
  
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

  const fetchInbound = useCallback(async () => {
    if (inboundEnabled === false) {
      setFaxes([]);
      setError(null);
      setHasLoaded(false);
      setLoading(false);
      return;
    }
    try {
      setError(null);
      setLoading(true);
      const data = await client.listInbound();
      setFaxes(data);
      setHasLoaded(true);
    } catch (err) {
      setHasLoaded(false);
      setError(err instanceof Error ? err.message : 'Failed to fetch inbound faxes');
    } finally {
      setLoading(false);
    }
  }, [client, inboundEnabled]);

  const fetchDeliveries = useCallback(async () => {
    if (inboundEnabled === false || !canReadDelivery) {
      setDeliveries(null);
      return;
    }
    try {
      setDeliveries((await client.listIntakeItems({ limit: 500 })).items);
    } catch {
      setDeliveries(null);
    }
    try {
      setConnectors((await client.listEmailConnectors()).connectors);
    } catch {
      setConnectors(null);
    }
  }, [client, inboundEnabled, canReadDelivery]);

  const fetchAgain = async (fax: InboundFax) => {
    setFetchingId(fax.id);
    setDeliveryError(null);
    setDeliveryNotice(null);
    try {
      await client.fetchInboundAgain(fax.id);
      setDeliveryNotice('Faxbot will fetch the document shortly.');
      await fetchInbound();
    } catch (failure) {
      setDeliveryError(failure);
    } finally {
      setFetchingId(null);
    }
  };

  const retryDelivery = async (item: IntakeItem) => {
    setRetrying(true);
    setDeliveryError(null);
    setDeliveryNotice(null);
    try {
      await client.retryIntakeItem(item.id);
      setDeliveryNotice('Faxbot will deliver it shortly.');
      await fetchDeliveries();
    } catch (failure) {
      setDeliveryError(failure);
    } finally {
      setRetrying(false);
    }
  };

  const deliveryFor = new Map((deliveries ?? []).filter((item) => item.inbound_fax_id).map((item) => [item.inbound_fax_id as string, item]));
  const directItems = (deliveries ?? []).filter((item) => item.source === 'direct');

  const fetchCallbacks = useCallback(async () => {
    setCallbacksError(null);
    if (inboundEnabled === false || !canReadProviders) {
      setCallbacks(null);
      return;
    }
    try {
      setCallbacks(await client.getInboundCallbacks());
    } catch {
      setCallbacks(null);
      setCallbacksError("Callback details couldn't be loaded. Select Refresh to try again.");
    }
  }, [client, inboundEnabled, canReadProviders]);

  const downloadPdf = async (id: string) => {
    try {
      const blob = await client.downloadInboundPdf(id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `inbound_${id}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(`Download failed: ${err instanceof Error ? err.message : 'Unknown error'}`);
    }
  };

  useEffect(() => {
    void fetchInbound();
    void fetchCallbacks();
    void fetchDeliveries();
  }, [fetchInbound, fetchCallbacks, fetchDeliveries]);

  useEffect(() => {
    if (inboundEnabled === false) return;
    // Auto-refresh inbound faxes and their delivery every 15 seconds
    const interval = setInterval(() => { void fetchInbound(); void fetchDeliveries(); }, 15000);
    return () => clearInterval(interval);
  }, [fetchInbound, fetchDeliveries, inboundEnabled]);

  const toneIcon = (tone: DeliveryTone) => {
    if (tone === 'success') return <SuccessIcon />;
    if (tone === 'error') return <ErrorIcon />;
    return <InfoIcon />;
  };

  // The fax's status chip with its one sentence underneath.
  const FaxStatus = ({ fax }: { fax: InboundFax }) => {
    const status = inboundFaxStatus(fax);
    return (
      <Box>
        <Chip
          icon={toneIcon(status.tone)}
          label={status.label}
          color={status.tone}
          size="small"
          variant="outlined"
          sx={{ borderRadius: 1 }}
        />
        {status.detail && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, maxWidth: 280 }}>
            {status.detail}
          </Typography>
        )}
      </Box>
    );
  };

  const FetchAgainButton = ({ fax, fullWidth = false }: { fax: InboundFax; fullWidth?: boolean }) => {
    if (!canAddTestFax || !inboundFaxStatus(fax).canFetchAgain) return null;
    return (
      <Button
        size="small"
        startIcon={<FetchIcon />}
        onClick={() => void fetchAgain(fax)}
        disabled={fetchingId !== null}
        fullWidth={fullWidth}
        aria-label={`Fetch again the fax from ${maskPhoneNumber(fax.fr)}`}
      >
        Fetch again
      </Button>
    );
  };

  const formatDate = (dateString?: string) => {
    if (!dateString) return '-';
    try {
      const date = parseServerTime(dateString);
      if (!date) return dateString;
      if (isSmallMobile) {
        return date.toLocaleDateString();
      }
      return date.toLocaleString();
    } catch {
      return dateString;
    }
  };

  // When the fax arrived (the provider's time when known), marked when Faxbot brought it in later.
  const arrivedText = (fax: InboundFax) => {
    const value = fax.source_received_at || fax.received_at;
    const date = parseServerTime(value);
    // Always with the time of day: a recovered fax arrived well before it was brought in.
    const when = date && isSmallMobile
      ? date.toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' })
      : formatDate(value);
    return fax.recovered ? `${when} · brought in later` : when;
  };

  const maskPhoneNumber = (phone?: string) => {
    // A number nobody reported is unknown, not hidden.
    if (!phone) return 'Unknown';
    if (phone.length < 4) return '****';
    return '*'.repeat(phone.length - 4) + phone.slice(-4);
  };

  const copyToClipboard = async (text: string, label: string = 'Copied') => {
    try {
      await navigator.clipboard.writeText(text);
      setCopySnackbar(label);
    } catch (err) {
      const textArea = document.createElement("textarea");
      textArea.value = text;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand('copy');
      document.body.removeChild(textArea);
      setCopySnackbar(label);
    }
  };

  const MobileFaxCard = ({ fax }: { fax: InboundFax }) => (
    <Grow in timeout={300}>
      <Card sx={{ mb: 2, borderRadius: 2 }}>
        <CardContent>
          <Stack spacing={2}>
            {/* Header */}
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <Typography variant="subtitle2" fontWeight={600}>
                Received through {providerName(fax.backend)}
              </Typography>
              <FaxStatus fax={fax} />
            </Box>

            {/* Details */}
            <Stack spacing={1}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <PhoneIcon fontSize="small" color="action" />
                <Box>
                  <Typography variant="caption" color="text.secondary">From:</Typography>
                  <Typography variant="body2" fontFamily="monospace" sx={{ ml: 1 }}>
                    {maskPhoneNumber(fax.fr)}
                  </Typography>
                </Box>
              </Box>

              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <PhoneIcon fontSize="small" color="action" />
                <Box>
                  <Typography variant="caption" color="text.secondary">To:</Typography>
                  <Typography variant="body2" fontFamily="monospace" sx={{ ml: 1 }}>
                    {maskPhoneNumber(fax.to)}
                  </Typography>
                </Box>
              </Box>

              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <DateIcon fontSize="small" color="action" />
                <Typography variant="body2">
                  {arrivedText(fax)}
                </Typography>
              </Box>

              {fax.pages && (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <DocumentIcon fontSize="small" color="action" />
                  <Typography variant="body2">
                    {fax.pages} {fax.pages === 1 ? 'page' : 'pages'}
                  </Typography>
                </Box>
              )}
            </Stack>

            {deliveries !== null && (
              <Box>
                <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 0.5 }}>Email delivery</Typography>
                <DeliveryStatusLine item={deliveryFor.get(fax.id)} canRetry={canRetryDelivery} busy={retrying} isNew={isNewFax(fax.received_at)}
                  emailApplies={emailDeliveryApplies(connectors, fax.to)} documentPending={!inboundFaxStatus(fax).hasDocument}
                  onRetry={(item) => void retryDelivery(item)} label={`the fax from ${maskPhoneNumber(fax.fr)}`} />
              </Box>
            )}

            {/* Actions */}
            <Tooltip title={inboundFaxStatus(fax).hasDocument ? '' : 'The document has not arrived yet.'}>
              <span>
                <Button
                  variant="contained"
                  startIcon={<DownloadIcon />}
                  onClick={() => downloadPdf(fax.id)}
                  disabled={!inboundFaxStatus(fax).hasDocument}
                  fullWidth
                  sx={{ borderRadius: 2 }}
                >
                  Download PDF
                </Button>
              </span>
            </Tooltip>
            <FetchAgainButton fax={fax} fullWidth />
          </Stack>
        </CardContent>
      </Card>
    </Grow>
  );

  return (
    <Box>
      <Box 
        display="flex" 
        justifyContent="space-between" 
        alignItems={{ xs: 'flex-start', sm: 'center' }}
        flexDirection={{ xs: 'column', sm: 'row' }}
        gap={2}
        mb={3}
      >
        <Box>
          <Typography variant="h4" component="h1">
            Inbound Faxes
          </Typography>
          {canOpenEmailSettings && (
            <Button variant="text" size="small" onClick={() => onNavigate?.('email')} sx={{ px: 0, minWidth: 0 }}>
              Email delivery settings
            </Button>
          )}
        </Box>
        <Box display="flex" gap={1}>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={() => {
              void fetchInbound();
              void fetchCallbacks();
              void fetchDeliveries();
            }}
            disabled={loading || inboundEnabled === false}
            size={isSmallMobile ? 'medium' : 'large'}
            sx={{ 
              borderRadius: 2,
              minHeight: isSmallMobile ? 40 : 42,
            }}
          >
            Refresh
          </Button>
          {canAddTestFax && <Button
            variant="outlined"
            startIcon={<TestIcon />}
            onClick={async () => { 
              if (inboundEnabled === false) return;
              try { 
                setSimulating(true); 
                await client.simulateInbound(); 
                await fetchInbound(); 
              } catch {
                setError("The test fax couldn't be added. Try again."); 
              } finally { 
                setSimulating(false);
              } 
            }}
            disabled={simulating || loading || inboundEnabled === false}
            size={isSmallMobile ? 'medium' : 'large'}
            sx={{ 
              borderRadius: 2,
              minHeight: isSmallMobile ? 40 : 42,
            }}
          >
            {isSmallMobile ? 'Test' : 'Add Test Fax'}
          </Button>}
        </Box>
      </Box>

      {inboundEnabled === false && (
        <Alert
          severity="info"
          sx={{ mb: 3, borderRadius: 2 }}
          action={onNavigate && <Button color="inherit" onClick={() => onNavigate('settings')}>Go to Settings</Button>}
        >
          Receiving faxes is disabled. Configure your receiving provider, then enable Inbound in Settings.
        </Alert>
      )}

      {error && (
        <Fade in>
          <Alert 
            severity="error" 
            sx={{ mb: 3, borderRadius: 2 }}
            onClose={() => setError(null)}
          >
            {error}
          </Alert>
        </Fade>
      )}

      <Notice message={deliveryNotice} onClose={() => setDeliveryNotice(null)} />
      <DeliveryError error={deliveryError} onClose={() => setDeliveryError(null)} />

      {/* Configuration Info */}
      {canReadProviders && <ResponsiveFormSection
        title="Inbound Fax Configuration"
        subtitle="Setup and requirements for receiving faxes"
        icon={<InboxIcon />}
      >
        <Stack spacing={2}>
          {callbacksError && <Alert severity="warning">{callbacksError}</Alert>}
          <Alert 
            severity="info" 
            sx={{ 
              borderRadius: 2,
              '& .MuiAlert-message': { width: '100%' }
            }}
          >
            <Typography variant="body2" fontWeight={500}>
              Requirements:
            </Typography>
            <Typography variant="body2" sx={{ mt: 0.5 }}>
              • You see the mailboxes you have been given access to<br />
              • Phone numbers are masked for HIPAA compliance
              {canAddTestFax && <><br />• "Add Test Fax" adds a one-page test document, marked as a test fax</>}
            </Typography>
          </Alert>

          {inboundEnabled !== false && callbacks && callbacks.callbacks && callbacks.callbacks.length > 0 && (
            <Box>
              <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>
                Provider Callback URLs
              </Typography>
              <Stack spacing={1}>
                {callbacks.callbacks.map((cb: any, idx: number) => (
                  <Paper
                    key={idx}
                    elevation={0}
                    sx={{
                      p: 2,
                      border: '1px solid',
                      borderColor: 'divider',
                      borderRadius: 2,
                      backgroundColor: theme.palette.mode === 'dark' 
                        ? 'rgba(255, 255, 255, 0.02)' 
                        : 'rgba(0, 0, 0, 0.02)',
                    }}
                  >
                    <Box sx={{ 
                      display: 'flex', 
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      flexWrap: 'wrap',
                      gap: 1
                    }}>
                      <Box sx={{ flex: 1 }}>
                        <Typography variant="body2" fontWeight={600}>
                          {cb.name}
                        </Typography>
                        <Typography 
                          variant="body2" 
                          sx={{ 
                            fontFamily: 'monospace',
                            wordBreak: 'break-all',
                            mt: 0.5
                          }}
                        >
                          {cb.url}
                        </Typography>
                      </Box>
                      <Button 
                        size="small" 
                        variant="outlined" 
                        startIcon={<ContentCopyIcon />} 
                        onClick={() => copyToClipboard(cb.url, `${cb.name} URL copied`)}
                        sx={{ borderRadius: 1 }}
                      >
                        Copy
                      </Button>
                    </Box>
                  </Paper>
                ))}
                <Typography variant="caption" color="text.secondary">
                  Configure these URLs in your provider console to deliver inbound faxes.
                </Typography>
              </Stack>
            </Box>
          )}

          {inboundEnabled !== false && callbacks && callbacks.backend === 'sip' && (
            <Alert severity={callbacks.receiving?.ready ? 'success' : 'warning'} sx={{ borderRadius: 2 }} data-testid="sip-receiving"
              action={onNavigate && permissions?.has('settings:read') ? (
                <Button color="inherit" size="small" onClick={() => onNavigate('trunk')}>Open trunk settings</Button>
              ) : undefined}>
              {callbacks.receiving?.message ?? 'Receiving over your SIP trunk.'}{' '}
              <Box component="a" href={docsLink('inbound', docsBase)} target="_blank" rel="noreferrer" sx={{ color: 'inherit' }}>How receiving works</Box>
            </Alert>
          )}
        </Stack>
      </ResponsiveFormSection>}

      {/* Faxes List */}
      {inboundEnabled !== false && (
      <Box sx={{ mt: 3 }}>
        {loading && faxes.length === 0 ? (
          <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 2 }}>
            <CircularProgress />
          </Paper>
        ) : faxes.length === 0 ? (
          <Fade in>
            <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 2 }}>
              <InboxIcon sx={{ fontSize: 48, color: 'text.secondary', mb: 2 }} />
              <Typography variant="h6" gutterBottom>
                {hasLoaded ? 'No Inbound Faxes' : 'Unable to Load Inbound Faxes'}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {hasLoaded ? 'Inbound faxes will appear here when received' : 'Use Refresh to retry. See the error above for details.'}
              </Typography>
            </Paper>
          </Fade>
        ) : isMobile ? (
          // Mobile Layout
          <Box>
            {faxes.map((fax) => (
              <MobileFaxCard key={fax.id} fax={fax} />
            ))}
          </Box>
        ) : (
          // Desktop Layout
          <Fade in>
            <Paper sx={{ borderRadius: 2 }}>
              <TableContainer>
                <Table>
                  <TableHead>
                    <TableRow>
                      <TableCell>From</TableCell>
                      <TableCell>To</TableCell>
                      <TableCell>Status</TableCell>
                      <TableCell>Received through</TableCell>
                      <TableCell>Pages</TableCell>
                      <TableCell>Received</TableCell>
                      {deliveries !== null && <TableCell>Email delivery</TableCell>}
                      <TableCell align="right">Actions</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {faxes.map((fax) => (
                      <TableRow key={fax.id} hover>
                        <TableCell>
                          <Typography variant="body2" fontFamily="monospace">
                            {maskPhoneNumber(fax.fr)}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2" fontFamily="monospace">
                            {maskPhoneNumber(fax.to)}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <FaxStatus fax={fax} />
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2">
                            {providerName(fax.backend)}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2">
                            {fax.pages || '-'}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="caption" color="text.secondary">
                            {arrivedText(fax)}
                          </Typography>
                        </TableCell>
                        {deliveries !== null && (
                          <TableCell>
                            <DeliveryStatusLine item={deliveryFor.get(fax.id)} canRetry={canRetryDelivery} busy={retrying} isNew={isNewFax(fax.received_at)}
                              emailApplies={emailDeliveryApplies(connectors, fax.to)} documentPending={!inboundFaxStatus(fax).hasDocument}
                              onRetry={(item) => void retryDelivery(item)} label={`the fax from ${maskPhoneNumber(fax.fr)}`} />
                          </TableCell>
                        )}
                        <TableCell align="right">
                          <Tooltip title={inboundFaxStatus(fax).hasDocument ? 'Download PDF' : 'The document has not arrived yet.'}>
                            <span>
                              <IconButton
                                size="small"
                                onClick={() => downloadPdf(fax.id)}
                                disabled={!fax.id || !inboundFaxStatus(fax).hasDocument}
                                aria-label={`Download the fax from ${maskPhoneNumber(fax.fr)}`}
                              >
                                <DownloadIcon />
                              </IconButton>
                            </span>
                          </Tooltip>
                          <FetchAgainButton fax={fax} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              
              {faxes.length > 0 && (
                <Box sx={{ p: 2, borderTop: '1px solid', borderColor: 'divider' }}>
                  <Typography variant="caption" color="text.secondary">
                    Auto-refreshing every 15 seconds • Phone numbers are masked for HIPAA compliance
                  </Typography>
                </Box>
              )}
            </Paper>
          </Fade>
        )}
      </Box>
      )}

      {inboundEnabled !== false && (
        <DirectDeliveries items={directItems} canRetry={canRetryDelivery} busy={retrying} onRetry={(item) => void retryDelivery(item)} />
      )}

      {/* Copy Snackbar */}
      <Snackbar
        open={!!copySnackbar}
        autoHideDuration={2000}
        onClose={() => setCopySnackbar('')}
        message={copySnackbar}
      />
    </Box>
  );
}

export default Inbound;
