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
  Alert,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Chip,
  CircularProgress,
  Paper,
  IconButton,
  Tooltip,
  useMediaQuery,
  useTheme,
  Stack,
  Card,
  CardContent,
  Fade,
  Grow,
  Snackbar,
} from '@mui/material';
import {
  Add as AddIcon,
  Refresh as RefreshIcon,
  Delete as DeleteIcon,
  Cached as RotateIcon,
  ContentCopy as CopyIcon,
  VpnKey as KeyIcon,
  Security as SecurityIcon,
  Person as PersonIcon,
  CalendarToday as DateIcon,
  CheckCircle as SuccessIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { ApiKey } from '../api/types';
import {
  ResponsiveTextField,
  ResponsiveFormSection,
} from './common/ResponsiveFormFields';

interface ApiKeysProps {
  client: AdminAPIClient;
}

function ApiKeys({ client }: ApiKeysProps) {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [newKeyResult, setNewKeyResult] = useState<string | null>(null);
  const [copySnackbar, setCopySnackbar] = useState(false);
  const [formData, setFormData] = useState({
    name: '',
    owner: '',
    scopes: 'fax:send,fax:read',
  });

  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

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

  const handleRotateKey = async (keyId: string, keyName?: string) => {
    if (!confirm(`Rotate key "${keyName || keyId}"? The old token will be invalidated.`)) {
      return;
    }
    
    try {
      const result = await client.rotateApiKey(keyId);
      setNewKeyResult(result.token);
      await fetchKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to rotate API key');
    }
  };

  const handleRevokeKey = async (keyId: string, keyName?: string) => {
    if (!confirm(`Revoke key "${keyName || keyId}"? This action cannot be undone.`)) {
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
      const date = new Date(dateString);
      if (isSmallMobile) {
        return date.toLocaleDateString();
      }
      return date.toLocaleString();
    } catch {
      return dateString;
    }
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopySnackbar(true);
    } catch (err) {
      // Fallback for older browsers
      const textArea = document.createElement("textarea");
      textArea.value = text;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand('copy');
      document.body.removeChild(textArea);
      setCopySnackbar(true);
    }
  };

  const MobileKeyCard = ({ apiKey }: { apiKey: ApiKey }) => (
    <Grow in timeout={300}>
      <Card sx={{ mb: 2, borderRadius: 2 }}>
        <CardContent>
          <Stack spacing={2}>
            {/* Header */}
            <Box>
              <Typography variant="h6" fontWeight={600}>
                {apiKey.name || 'Unnamed Key'}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                ID: {apiKey.key_id}
              </Typography>
            </Box>

            {/* Details */}
            <Stack spacing={1}>
              {apiKey.owner && (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <PersonIcon fontSize="small" color="action" />
                  <Typography variant="body2">{apiKey.owner}</Typography>
                </Box>
              )}
              
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <DateIcon fontSize="small" color="action" />
                <Typography variant="body2">
                  Created: {formatDate(apiKey.created_at)}
                </Typography>
              </Box>

              {apiKey.last_used_at && (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <DateIcon fontSize="small" color="action" />
                  <Typography variant="body2">
                    Last used: {formatDate(apiKey.last_used_at)}
                </Typography>
                </Box>
              )}
            </Stack>

            {/* Scopes */}
            <Box>
              <Typography variant="caption" color="text.secondary" sx={{ mb: 0.5, display: 'block' }}>
                Permissions:
              </Typography>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                {apiKey.scopes?.map((scope) => (
                  <Chip
                    key={scope}
                    label={scope}
                    size="small"
                    variant="outlined"
                    sx={{ borderRadius: 1 }}
                  />
                ))}
              </Box>
            </Box>

            {/* Actions */}
            <Box sx={{ display: 'flex', gap: 1, pt: 1 }}>
              <Button
                variant="outlined"
                size="small"
                startIcon={<RotateIcon />}
                onClick={() => handleRotateKey(apiKey.key_id, apiKey.name)}
                sx={{ flex: 1 }}
              >
                Rotate
              </Button>
              <Button
                variant="outlined"
                size="small"
                color="error"
                startIcon={<DeleteIcon />}
                onClick={() => handleRevokeKey(apiKey.key_id, apiKey.name)}
                sx={{ flex: 1 }}
              >
                Revoke
              </Button>
            </Box>
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
          API Keys
        </Typography>
        <Box display="flex" gap={1}>
          <Button
            variant="outlined"
            startIcon={<RefreshIcon />}
            onClick={fetchKeys}
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
            variant="contained"
            startIcon={<AddIcon />}
            onClick={() => setCreateDialogOpen(true)}
            size={isSmallMobile ? 'medium' : 'large'}
            sx={{ 
              borderRadius: 2,
              minHeight: isSmallMobile ? 40 : 42,
            }}
          >
            Create Key
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

      {loading ? (
        <Box display="flex" justifyContent="center" py={4}>
          <CircularProgress />
        </Box>
      ) : keys.length === 0 ? (
        <Fade in>
          <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 2 }}>
            <KeyIcon sx={{ fontSize: 48, color: 'text.secondary', mb: 2 }} />
            <Typography variant="h6" gutterBottom>
              No API Keys
            </Typography>
            <Typography color="text.secondary" sx={{ mb: 3 }}>
              Create your first API key to start using the Faxbot API
            </Typography>
            <Button
              variant="contained"
              startIcon={<AddIcon />}
              onClick={() => setCreateDialogOpen(true)}
              sx={{ borderRadius: 2 }}
            >
              Create Your First Key
            </Button>
          </Paper>
        </Fade>
      ) : isMobile ? (
        // Mobile Layout
        <Box>
          {keys.map((key) => (
            <MobileKeyCard key={key.key_id} apiKey={key} />
          ))}
        </Box>
      ) : (
        // Desktop Layout
        <Fade in>
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Name / ID</TableCell>
                  <TableCell>Owner</TableCell>
                  <TableCell>Scopes</TableCell>
                  <TableCell>Created</TableCell>
                  <TableCell>Last Used</TableCell>
                  <TableCell align="right">Actions</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {keys.map((key) => (
                  <TableRow key={key.key_id} hover>
                    <TableCell>
                      <Box>
                        <Typography variant="body2" fontWeight={600}>
                          {key.name || 'Unnamed'}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {key.key_id}
                        </Typography>
                      </Box>
                    </TableCell>
                    <TableCell>{key.owner || '-'}</TableCell>
                    <TableCell>
                      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                        {key.scopes?.map((scope) => (
                          <Chip
                            key={scope}
                            label={scope}
                            size="small"
                            variant="outlined"
                            sx={{ borderRadius: 1 }}
                          />
                        ))}
                      </Box>
                    </TableCell>
                    <TableCell>{formatDate(key.created_at)}</TableCell>
                    <TableCell>{formatDate(key.last_used_at)}</TableCell>
                    <TableCell align="right">
                      <Tooltip title="Rotate Key">
                        <IconButton
                          onClick={() => handleRotateKey(key.key_id, key.name)}
                          size="small"
                        >
                          <RotateIcon />
                        </IconButton>
                      </Tooltip>
                      <Tooltip title="Revoke Key">
                        <IconButton
                          onClick={() => handleRevokeKey(key.key_id, key.name)}
                          size="small"
                          color="error"
                        >
                          <DeleteIcon />
                        </IconButton>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Fade>
      )}

      {/* Create Key Dialog */}
      <Dialog 
        open={createDialogOpen} 
        onClose={() => !newKeyResult && setCreateDialogOpen(false)}
        maxWidth="sm"
        fullWidth
        fullScreen={isSmallMobile}
      >
        <DialogTitle>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <KeyIcon />
            Create New API Key
          </Box>
        </DialogTitle>
        <DialogContent>
          {!newKeyResult ? (
            <Box sx={{ pt: 1 }}>
              <ResponsiveTextField
                label="Key Name"
                value={formData.name}
                onChange={(value) => setFormData({ ...formData, name: value })}
                placeholder="Production API Key"
                helperText="A friendly name to identify this key"
                icon={<KeyIcon />}
              />
              
              <ResponsiveTextField
                label="Owner"
                value={formData.owner}
                onChange={(value) => setFormData({ ...formData, owner: value })}
                placeholder="service@example.com"
                helperText="Email or identifier of the key owner"
                icon={<PersonIcon />}
              />
              
              <ResponsiveTextField
                label="Permissions (Scopes)"
                value={formData.scopes}
                onChange={(value) => setFormData({ ...formData, scopes: value })}
                placeholder="fax:send,fax:read"
                helperText="Comma-separated list of permissions. Available: fax:send, fax:read, inbound:list, inbound:read, keys:manage"
                icon={<SecurityIcon />}
              />
            </Box>
          ) : (
            <Box>
              <Alert 
                severity="success" 
                icon={<SuccessIcon />}
                sx={{ mb: 3, borderRadius: 2 }}
              >
                API key created successfully! Copy it now - you won't be able to see it again.
              </Alert>
              
              <ResponsiveFormSection
                title="Your New API Key"
                subtitle="Save this key securely - it won't be shown again"
                icon={<KeyIcon />}
              >
                <Box
                  sx={{
                    p: 2,
                    backgroundColor: theme.palette.mode === 'dark'
                      ? 'rgba(255, 255, 255, 0.05)'
                      : 'rgba(0, 0, 0, 0.05)',
                    borderRadius: 1,
                    fontFamily: 'monospace',
                    wordBreak: 'break-all',
                    position: 'relative',
                  }}
                >
                  <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                    {newKeyResult}
                  </Typography>
                  <IconButton
                    onClick={() => copyToClipboard(newKeyResult)}
                    size="small"
                    sx={{
                      position: 'absolute',
                      top: 8,
                      right: 8,
                    }}
                  >
                    <CopyIcon fontSize="small" />
                  </IconButton>
                </Box>
              </ResponsiveFormSection>
            </Box>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 3 }}>
          {!newKeyResult ? (
            <>
              <Button 
                onClick={() => setCreateDialogOpen(false)}
                sx={{ borderRadius: 2 }}
              >
                Cancel
              </Button>
              <Button 
                onClick={handleCreateKey} 
                variant="contained"
                startIcon={<AddIcon />}
                sx={{ borderRadius: 2 }}
              >
                Create Key
              </Button>
            </>
          ) : (
            <>
              <Button
                onClick={() => copyToClipboard(newKeyResult)}
                startIcon={<CopyIcon />}
                sx={{ borderRadius: 2 }}
              >
                Copy Key
              </Button>
              <Button
                onClick={() => {
                  setNewKeyResult(null);
                  setCreateDialogOpen(false);
                }}
                variant="contained"
                sx={{ borderRadius: 2 }}
              >
                Done
              </Button>
            </>
          )}
        </DialogActions>
      </Dialog>

      {/* Copy Snackbar */}
      <Snackbar
        open={copySnackbar}
        autoHideDuration={2000}
        onClose={() => setCopySnackbar(false)}
        message="Copied to clipboard!"
      />
    </Box>
  );
}

export default ApiKeys;