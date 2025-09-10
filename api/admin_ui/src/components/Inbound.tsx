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
import {
  Refresh as RefreshIcon,
  Download as DownloadIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { InboundFax } from '../api/types';

interface InboundProps {
  client: AdminAPIClient;
}

function Inbound({ client }: InboundProps) {
  const [faxes, setFaxes] = useState<InboundFax[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
        <Button
          variant="outlined"
          startIcon={<RefreshIcon />}
          onClick={fetchInbound}
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

      <Alert severity="info" sx={{ mb: 3 }}>
        <Typography variant="body2">
          <strong>Note:</strong> Requires inbound:list and inbound:read scopes or bootstrap key. 
          Phone numbers are masked for HIPAA compliance.
        </Typography>
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
