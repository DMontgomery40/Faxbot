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
  TextField,
  Grid,
  Alert,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Chip,
  CircularProgress,
} from '@mui/material';
import {
  Add as AddIcon,
  Refresh as RefreshIcon,
  Delete as DeleteIcon,
  Cached as CachedIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { ApiKey } from '../api/types';

interface ApiKeysProps {
  client: AdminAPIClient;
}

function ApiKeys({ client }: ApiKeysProps) {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [newKeyResult, setNewKeyResult] = useState<string | null>(null);
  const [formData, setFormData] = useState({
    name: '',
    owner: '',
    scopes: 'fax:send,fax:read',
  });

  const fetchKeys = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.listApiKeys();
      setKeys(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch API keys');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchKeys();
  }, [client]);

  const handleCreateKey = async () => {
    try {
      const scopes = formData.scopes.split(',').map(s => s.trim()).filter(Boolean);
      const result = await client.createApiKey({
        name: formData.name || undefined,
        owner: formData.owner || undefined,
        scopes,
      });
      
      setNewKeyResult(result.token);
      setFormData({ name: '', owner: '', scopes: 'fax:send,fax:read' });
      await fetchKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create API key');
    }
  };

  const handleRotateKey = async (keyId: string) => {
    if (!confirm(`Rotate key ${keyId}? The old token will be invalidated.`)) {
      return;
    }
    
    try {
      const result = await client.rotateApiKey(keyId);
      alert(`New token (copy now):\n${result.token}`);
      await fetchKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to rotate API key');
    }
  };

  const handleRevokeKey = async (keyId: string) => {
    if (!confirm(`Revoke key ${keyId}? This action cannot be undone.`)) {
      return;
    }
    
    try {
      await client.revokeApiKey(keyId);
      await fetchKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke API key');
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

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          API Keys
        </Typography>
        <Box>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchKeys}
            disabled={loading}
            sx={{ mr: 1 }}
          >
            Refresh
          </Button>
          <Button
            variant="contained"
            startIcon={<AddIcon />}
            onClick={() => setCreateDialogOpen(true)}
          >
            Create Key
          </Button>
        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      <Card>
        <CardContent>
          {loading ? (
            <Box display="flex" justifyContent="center" py={4}>
              <CircularProgress />
            </Box>
          ) : keys.length === 0 ? (
            <Box textAlign="center" py={4}>
              <Typography variant="body1" color="text.secondary">
                No API keys found
              </Typography>
            </Box>
          ) : (
            <TableContainer>
              <Table>
                <TableHead>
                  <TableRow>
                    <TableCell>Key ID</TableCell>
                    <TableCell>Name</TableCell>
                    <TableCell>Scopes</TableCell>
                    <TableCell>Owner</TableCell>
                    <TableCell>Created</TableCell>
                    <TableCell>Last Used</TableCell>
                    <TableCell>Expires</TableCell>
                    <TableCell>Actions</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {keys.map((key) => (
                    <TableRow key={key.key_id} hover>
                      <TableCell>
                        <Typography variant="body2" fontFamily="monospace">
                          {key.key_id}
                        </Typography>
                      </TableCell>
                      <TableCell>{key.name || '-'}</TableCell>
                      <TableCell>
                        <Box display="flex" flexWrap="wrap" gap={0.5}>
                          {key.scopes.map((scope) => (
                            <Chip
                              key={scope}
                              label={scope}
                              size="small"
                              variant="outlined"
                            />
                          ))}
                        </Box>
                      </TableCell>
                      <TableCell>{key.owner || '-'}</TableCell>
                      <TableCell>
                        <Typography variant="caption" color="text.secondary">
                          {formatDate(key.created_at)}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" color="text.secondary">
                          {formatDate(key.last_used_at)}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" color="text.secondary">
                          {formatDate(key.expires_at)}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Box display="flex" gap={1}>
                          <Button
                            size="small"
                            startIcon={<CachedIcon />}
                            onClick={() => handleRotateKey(key.key_id)}
                          >
                            Rotate
                          </Button>
                          <Button
                            size="small"
                            color="error"
                            startIcon={<DeleteIcon />}
                            onClick={() => handleRevokeKey(key.key_id)}
                          >
                            Revoke
                          </Button>
                        </Box>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </CardContent>
      </Card>

      {/* Create Key Dialog */}
      <Dialog open={createDialogOpen} onClose={() => setCreateDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Create API Key</DialogTitle>
        <DialogContent>
          <Grid container spacing={2} sx={{ mt: 1 }}>
            <Grid item xs={12}>
              <TextField
                fullWidth
                label="Name (optional)"
                value={formData.name}
                onChange={(e) => setFormData(prev => ({ ...prev, name: e.target.value }))}
                placeholder="e.g., Admin Console"
              />
            </Grid>
            <Grid item xs={12}>
              <TextField
                fullWidth
                label="Owner (optional)"
                value={formData.owner}
                onChange={(e) => setFormData(prev => ({ ...prev, owner: e.target.value }))}
                placeholder="e.g., ops@clinic.com"
              />
            </Grid>
            <Grid item xs={12}>
              <TextField
                fullWidth
                label="Scopes"
                value={formData.scopes}
                onChange={(e) => setFormData(prev => ({ ...prev, scopes: e.target.value }))}
                placeholder="fax:send,fax:read,keys:manage"
                helperText="Comma-separated list of scopes"
              />
            </Grid>
          </Grid>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateDialogOpen(false)}>Cancel</Button>
          <Button onClick={handleCreateKey} variant="contained">
            Create
          </Button>
        </DialogActions>
      </Dialog>

      {/* New Key Result Dialog */}
      <Dialog open={!!newKeyResult} onClose={() => setNewKeyResult(null)} maxWidth="sm" fullWidth>
        <DialogTitle>API Key Created</DialogTitle>
        <DialogContent>
          <Alert severity="warning" sx={{ mb: 2 }}>
            <strong>Important:</strong> This token will only be shown once. Copy it now and store it securely.
          </Alert>
          <TextField
            fullWidth
            multiline
            rows={4}
            value={newKeyResult || ''}
            InputProps={{
              readOnly: true,
              style: { fontFamily: 'monospace' },
            }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => copyToClipboard(newKeyResult || '')}>
            Copy Token
          </Button>
          <Button onClick={() => setNewKeyResult(null)} variant="contained">
            Done
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

export default ApiKeys;
