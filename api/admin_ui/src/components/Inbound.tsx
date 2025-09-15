import { useState, useEffect } from 'react';
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
  Button,
  CircularProgress,
  Alert,
  Chip,
} from '@mui/material';
import { Refresh as RefreshIcon, Download as DownloadIcon, ContentCopy as ContentCopyIcon, PlayArrow as PlayArrowIcon } from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { InboundFax } from '../api/types';

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
  const [copied, setCopied] = useState(false);

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
      return new Date(dateString).toLocaleString();
    } catch {
      return dateString;
    }
  };

  const maskPhoneNumber = (phone?: string) => {
    if (!phone || phone.length < 4) return '****';
    return '*'.repeat(phone.length - 4) + phone.slice(-4);
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          Inbound Faxes
        </Typography>
        <Box>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchInbound}
            disabled={loading}
            sx={{ mr: 1 }}
          >
            Refresh
          </Button>
          <Button
            variant="outlined"
            startIcon={<PlayArrowIcon />}
            onClick={async () => { try { setSimulating(true); await client.simulateInbound(); await fetchInbound(); } catch(e:any){ setError(e?.message||'Test add failed'); } finally { setSimulating(false);} }}
            disabled={simulating}
          >
            Add Test Fax
          </Button>
        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      <Alert severity="info" sx={{ mb: 3 }}>
        <Typography variant="body2">
          <strong>Note:</strong> Requires inbound:list and inbound:read scopes or bootstrap key. Phone numbers are masked for HIPAA compliance.
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          "Add Test Fax" creates a local test entry only — use your provider console with the callback URL below for real inbound delivery.
        </Typography>
        {callbacks && callbacks.callbacks && callbacks.callbacks.length > 0 && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2" gutterBottom>
              Provider Callback URL
            </Typography>
            {callbacks.callbacks.map((cb: any, idx: number) => (
              <Box key={idx} display="flex" alignItems="center" sx={{ mb: 1 }}>
                <Typography variant="body2" sx={{ mr: 1 }}>
                  {cb.name}: {cb.url}
                </Typography>
                <Button size="small" variant="outlined" startIcon={<ContentCopyIcon />} onClick={() => navigator.clipboard.writeText(cb.url)}>
                  Copy
                </Button>
              </Box>
            ))}
            <Typography variant="caption" color="text.secondary">
              Configure this URL in your provider console to deliver inbound faxes.
            </Typography>
            {callbacks.backend === 'sip' && (
              <Box sx={{ mt: 2 }}>
                <Typography variant="subtitle2" gutterBottom>
                  Asterisk inbound (internal)
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                  Add a dialplan step after ReceiveFAX to POST the TIFF path internally:
                </Typography>
                <Box component="pre" sx={{ p: 1, bgcolor: 'background.default', border: '1px solid', borderColor: 'divider', borderRadius: 1, overflowX: 'auto', fontSize: '0.75rem' }}>
{`same => n,Set(FAXFILE=/faxdata/${'${UNIQUEID}'}.tiff)
same => n,ReceiveFAX(${"${FAXFILE}"})
same => n,Set(FAXSTATUS=${'${FAXOPT(status)}'})
same => n,Set(FAXPAGES=${'${FAXOPT(pages)}'})
same => n,System(curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Internal-Secret: YOUR_SECRET" \
  -d "{\"tiff_path\":\"${'${FAXFILE}'}\",\"to_number\":\"${'${EXTEN}'}\",\"from_number\":\"${'${CALLERID(num)}'}\",\"faxstatus\":\"${'${FAXSTATUS}'}\",\"faxpages\":\"${'${FAXPAGES}'}\",\"uniqueid\":\"${'${UNIQUEID}'}\"}" \
  http://api:8080/_internal/asterisk/inbound)`}
                </Box>
                <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
                  <Button size="small" variant="outlined" startIcon={<ContentCopyIcon />} onClick={async () => { await navigator.clipboard.writeText(
`same => n,Set(FAXFILE=/faxdata/${'${UNIQUEID}'}.tiff)
same => n,ReceiveFAX(${"${FAXFILE}"})
same => n,Set(FAXSTATUS=${'${FAXOPT(status)}'})
same => n,Set(FAXPAGES=${'${FAXOPT(pages)}'})
same => n,System(curl -s -X POST -H "Content-Type: application/json" -H "X-Internal-Secret: YOUR_SECRET" -d "{\"tiff_path\":\"${'${FAXFILE}'}\",\"to_number\":\"${'${EXTEN}'}\",\"from_number\":\"${'${CALLERID(num)}'}\",\"faxstatus\":\"${'${FAXSTATUS}'}\",\"faxpages\":\"${'${FAXPAGES}'}\",\"uniqueid\":\"${'${UNIQUEID}'}\"}" http://api:8080/_internal/asterisk/inbound)`
                  ); setCopied(true); setTimeout(()=>setCopied(false), 2000); }}>
                    {copied ? 'Copied' : 'Copy dialplan snippet'}
                  </Button>
                  <Button size="small" href={`${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/sip-setup.html#inbound`} target="_blank" rel="noreferrer">
                    Learn more (Asterisk inbound)
                  </Button>
                </Box>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                  Use service name "api" when running via Docker Compose; otherwise, point to your API host. Ensure Asterisk mounts the same /faxdata volume.
                </Typography>
              </Box>
            )}
          </Box>
        )}
      </Alert>

      <Card>
        <CardContent>
          {loading && faxes.length === 0 ? (
            <Box display="flex" justifyContent="center" py={4}>
              <CircularProgress />
            </Box>
          ) : faxes.length === 0 ? (
            <Box textAlign="center" py={4}>
              <Typography variant="body1" color="text.secondary">
                No inbound faxes found
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                {error ? 'Check your API key permissions' : 'Inbound faxes will appear here when received'}
              </Typography>
            </Box>
          ) : (
            <TableContainer sx={{ overflowX: 'auto' }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ minWidth: 120 }}>ID</TableCell>
                    <TableCell sx={{ minWidth: 80 }}>From</TableCell>
                    <TableCell sx={{ minWidth: 80, display: { xs: 'none', sm: 'table-cell' } }}>To</TableCell>
                    <TableCell sx={{ minWidth: 80 }}>Status</TableCell>
                    <TableCell sx={{ minWidth: 80, display: { xs: 'none', md: 'table-cell' } }}>Backend</TableCell>
                    <TableCell sx={{ minWidth: 60, display: { xs: 'none', md: 'table-cell' } }}>Pages</TableCell>
                    <TableCell sx={{ minWidth: 120, display: { xs: 'none', sm: 'table-cell' } }}>Received</TableCell>
                    <TableCell sx={{ minWidth: 80 }}>PDF</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {faxes.map((fax) => (
                    <TableRow key={fax.id} hover>
                      <TableCell>
                        <Typography variant="body2" fontFamily="monospace" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {fax.id.slice(0, 8)}...
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" fontFamily="monospace" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {maskPhoneNumber(fax.fr)}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                        <Typography variant="body2" fontFamily="monospace" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {maskPhoneNumber(fax.to)}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Chip
                          label={fax.status}
                          color={getStatusColor(fax.status)}
                          size="small"
                          variant="outlined"
                          sx={{ fontSize: { xs: '0.6rem', sm: '0.75rem' } }}
                        />
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {fax.backend}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {fax.pages || '-'}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                        <Typography variant="caption" color="text.secondary" sx={{ fontSize: { xs: '0.6rem', sm: '0.75rem' } }}>
                          {formatDate(fax.received_at)?.split(' ')[0]}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Button
                          size="small"
                          startIcon={window.innerWidth > 600 ? <DownloadIcon /> : undefined}
                          onClick={() => downloadPdf(fax.id)}
                          disabled={!fax.id}
                          sx={{ 
                            minWidth: { xs: 'auto', sm: 'auto' },
                            fontSize: { xs: '0.7rem', sm: '0.875rem' },
                            px: { xs: 1, sm: 2 }
                          }}
                        >
                          {window.innerWidth > 600 ? 'PDF' : '📄'}
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
          
          {faxes.length > 0 && (
            <Box mt={2}>
              <Typography variant="caption" color="text.secondary">
                Auto-refreshing every 15 seconds • Phone numbers are masked for HIPAA compliance
              </Typography>
            </Box>
          )}
        </CardContent>
      </Card>
    </Box>
  );
}

export default Inbound;
