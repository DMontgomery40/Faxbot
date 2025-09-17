import { useState, useEffect } from 'react';
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
  Warning as WarningIcon,
  Info as InfoIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { InboundFax } from '../api/types';
import { ResponsiveFormSection } from './common/ResponsiveFormFields';

interface InboundProps {
  client: AdminAPIClient;
  docsBase?: string;
}

function Inbound({ client, docsBase }: InboundProps) {
  const [faxes, setFaxes] = useState<InboundFax[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [callbacks, setCallbacks] = useState<any | null>(null);
  const [simulating, setSimulating] = useState(false);
  const [copySnackbar, setCopySnackbar] = useState<string>('');
  
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

  const fetchInbound = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.listInbound();
      setFaxes(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch inbound faxes');
    } finally {
      setLoading(false);
    }
  };

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
    fetchInbound();
    (async () => {
      try { setCallbacks(await client.getInboundCallbacks()); } catch {}
    })();
  }, [client]);

  useEffect(() => {
    // Auto-refresh inbound faxes every 15 seconds
    const interval = setInterval(fetchInbound, 15000);
    return () => clearInterval(interval);
  }, [client]);

  const getStatusIcon = (status: string) => {
    switch (status.toLowerCase()) {
      case 'success':
      case 'completed':
      case 'received':
        return <SuccessIcon />;
      case 'failed':
      case 'error':
        return <ErrorIcon />;
      case 'processing':
        return <WarningIcon />;
      default:
        return <InfoIcon />;
    }
  };

  const getStatusColor = (status: string): 'success' | 'error' | 'warning' | 'info' | 'default' => {
    switch (status.toLowerCase()) {
      case 'success':
      case 'completed':
      case 'received':
        return 'success';
      case 'failed':
      case 'error':
        return 'error';
      case 'processing':
        return 'warning';
      default:
        return 'default';
    }
  };

  const formatDate = (dateString?: string) => {
    if (!dateString) return '-';
    try {
      const date = new Date(dateString);
      if (isSmallMobile) {
        return date.toLocaleDateString();
      }
      return date.toLocaleString();
    } catch {
      return dateString;
    }
  };

  const maskPhoneNumber = (phone?: string) => {
    if (!phone || phone.length < 4) return '****';
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
              <Box>
                <Typography variant="subtitle2" fontWeight={600}>
                  Fax ID
                </Typography>
                <Typography variant="caption" color="text.secondary" fontFamily="monospace">
                  {fax.id.slice(0, 8)}...
                </Typography>
              </Box>
              <Chip
                icon={getStatusIcon(fax.status)}
                label={fax.status}
                color={getStatusColor(fax.status)}
                size="small"
                variant="outlined"
                sx={{ borderRadius: 1 }}
              />
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
                  {formatDate(fax.received_at)}
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

            {/* Actions */}
            <Button
              variant="contained"
              startIcon={<DownloadIcon />}
              onClick={() => downloadPdf(fax.id)}
              fullWidth
              sx={{ borderRadius: 2 }}
            >
              Download PDF
            </Button>
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
        <Typography variant="h4" component="h1">
          Inbound Faxes
        </Typography>
        <Box display="flex" gap={1}>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchInbound}
            disabled={loading}
            size={isSmallMobile ? 'medium' : 'large'}
            sx={{ 
              borderRadius: 2,
              minHeight: isSmallMobile ? 40 : 42,
            }}
          >
            Refresh
          </Button>
          <Button
            variant="outlined"
            startIcon={<TestIcon />}
            onClick={async () => { 
              try { 
                setSimulating(true); 
                await client.simulateInbound(); 
                await fetchInbound(); 
              } catch(e:any){ 
                setError(e?.message||'Test add failed'); 
              } finally { 
                setSimulating(false);
              } 
            }}
            disabled={simulating}
            size={isSmallMobile ? 'medium' : 'large'}
            sx={{ 
              borderRadius: 2,
              minHeight: isSmallMobile ? 40 : 42,
            }}
          >
            {isSmallMobile ? 'Test' : 'Add Test Fax'}
          </Button>
        </Box>
      </Box>

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

      {/* Configuration Info */}
      <ResponsiveFormSection
        title="Inbound Fax Configuration"
        subtitle="Setup and requirements for receiving faxes"
        icon={<InboxIcon />}
      >
        <Stack spacing={2}>
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
              • Requires <code>inbound:list</code> and <code>inbound:read</code> scopes or bootstrap key<br />
              • Phone numbers are masked for HIPAA compliance<br />
              • "Add Test Fax" creates local test entries only
            </Typography>
          </Alert>

          {callbacks && callbacks.callbacks && callbacks.callbacks.length > 0 && (
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

          {callbacks && callbacks.backend === 'sip' && (
            <Box>
              <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>
                Asterisk Inbound Configuration
              </Typography>
              <Paper
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
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                  Add this dialplan step after ReceiveFAX to POST the TIFF path internally:
                </Typography>
                <Box 
                  component="pre" 
                  sx={{ 
                    p: 2, 
                    bgcolor: 'background.default', 
                    border: '1px solid', 
                    borderColor: 'divider', 
                    borderRadius: 1, 
                    overflowX: 'auto', 
                    fontSize: isSmallMobile ? '0.7rem' : '0.75rem',
                    fontFamily: 'monospace',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all'
                  }}
                >
{`same => n,Set(FAXFILE=/faxdata/${'${UNIQUEID}'}.tiff)
same => n,ReceiveFAX(${"${FAXFILE}"})
same => n,Set(FAXSTATUS=${'${FAXOPT(status)}'})
same => n,Set(FAXPAGES=${'${FAXOPT(pages)}'})
same => n,System(curl -s -X POST \\
  -H "Content-Type: application/json" \\
  -H "X-Internal-Secret: YOUR_SECRET" \\
  -d "{\\"tiff_path\\":\\"${'${FAXFILE}'}\\",\\"to_number\\":\\"${'${EXTEN}'}\\",\\"from_number\\":\\"${'${CALLERID(num)'}'}\\",\\"faxstatus\\":\\"${'${FAXSTATUS}'}\\",\\"faxpages\\":\\"${'${FAXPAGES}'}\\",\\"uniqueid\\":\\"${'${UNIQUEID}'}\\"}" \\
  http://api:8080/_internal/asterisk/inbound)`}
                </Box>
                <Box sx={{ 
                  display: 'flex', 
                  gap: 1, 
                  mt: 2,
                  flexDirection: { xs: 'column', sm: 'row' }
                }}>
                  <Button 
                    size="small" 
                    variant="outlined" 
                    startIcon={<ContentCopyIcon />} 
                    onClick={() => copyToClipboard(
`same => n,Set(FAXFILE=/faxdata/${'${UNIQUEID}'}.tiff)
same => n,ReceiveFAX(${"${FAXFILE}"})
same => n,Set(FAXSTATUS=${'${FAXOPT(status)}'})
same => n,Set(FAXPAGES=${'${FAXOPT(pages)}'})
same => n,System(curl -s -X POST -H "Content-Type: application/json" -H "X-Internal-Secret: YOUR_SECRET" -d "{\\"tiff_path\\":\\"${'${FAXFILE}'}\\",\\"to_number\\":\\"${'${EXTEN}'}\\",\\"from_number\\":\\"${'${CALLERID(num)'}'}\\",\\"faxstatus\\":\\"${'${FAXSTATUS}'}\\",\\"faxpages\\":\\"${'${FAXPAGES}'}\\",\\"uniqueid\\":\\"${'${UNIQUEID}'}\\"}" http://api:8080/_internal/asterisk/inbound)`, 
                      'Dialplan snippet copied'
                    )}
                    fullWidth={isSmallMobile}
                    sx={{ borderRadius: 1 }}
                  >
                    Copy dialplan
                  </Button>
                  <Button 
                    size="small" 
                    href={`${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/sip-setup.html#inbound-receiving-quickstart-wip`} 
                    target="_blank" 
                    rel="noreferrer"
                    fullWidth={isSmallMobile}
                    sx={{ borderRadius: 1 }}
                  >
                    Learn more
                  </Button>
                </Box>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 2 }}>
                  Use service name "api" when running via Docker Compose; otherwise, point to your API host. Ensure Asterisk mounts the same /faxdata volume.
                </Typography>
              </Paper>
            </Box>
          )}
        </Stack>
      </ResponsiveFormSection>

      {/* Faxes List */}
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
                No Inbound Faxes
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {error ? 'Check your API key permissions' : 'Inbound faxes will appear here when received'}
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
                      <TableCell>ID</TableCell>
                      <TableCell>From</TableCell>
                      <TableCell>To</TableCell>
                      <TableCell>Status</TableCell>
                      <TableCell>Backend</TableCell>
                      <TableCell>Pages</TableCell>
                      <TableCell>Received</TableCell>
                      <TableCell align="right">Actions</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {faxes.map((fax) => (
                      <TableRow key={fax.id} hover>
                        <TableCell>
                          <Typography variant="body2" fontFamily="monospace">
                            {fax.id.slice(0, 8)}...
                          </Typography>
                        </TableCell>
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
                          <Chip
                            icon={getStatusIcon(fax.status)}
                            label={fax.status}
                            color={getStatusColor(fax.status)}
                            size="small"
                            variant="outlined"
                            sx={{ borderRadius: 1 }}
                          />
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2">
                            {fax.backend}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2">
                            {fax.pages || '-'}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="caption" color="text.secondary">
                            {formatDate(fax.received_at)}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          <Tooltip title="Download PDF">
                            <IconButton
                              size="small"
                              onClick={() => downloadPdf(fax.id)}
                              disabled={!fax.id}
                            >
                              <DownloadIcon />
                            </IconButton>
                          </Tooltip>
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