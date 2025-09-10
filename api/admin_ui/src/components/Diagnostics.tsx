import { useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Button,
  Alert,
  Paper,
  CircularProgress,
  Chip,
  List,
  ListItem,
  ListItemIcon,
  ListItemText,
} from '@mui/material';
import {
  PlayArrow as PlayArrowIcon,
  Download as DownloadIcon,
  ContentCopy as ContentCopyIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  Warning as WarningIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { DiagnosticsResult } from '../api/types';

interface DiagnosticsProps {
  client: AdminAPIClient;
}

function Diagnostics({ client }: DiagnosticsProps) {
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runDiagnostics = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.runDiagnostics();
      setDiagnostics(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to run diagnostics');
    } finally {
      setLoading(false);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  const downloadText = (filename: string, text: string) => {
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const renderCheckValue = (value: any) => {
    if (typeof value === 'boolean') {
      return (
        <Chip
          label={value ? 'Pass' : 'Fail'}
          color={value ? 'success' : 'error'}
          size="small"
          variant="outlined"
        />
      );
    }
    return <Typography variant="body2">{String(value)}</Typography>;
  };

  const renderChecks = (checks: Record<string, any>, title: string) => {
    return (
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            {title}
          </Typography>
          {Object.entries(checks).map(([key, value]) => (
            <Box key={key} display="flex" justifyContent="space-between" alignItems="center" mb={1}>
              <Typography variant="body2">{key.replace(/_/g, ' ')}:</Typography>
              {renderCheckValue(value)}
            </Box>
          ))}
        </CardContent>
      </Card>
    );
  };

  const getSuggestions = (diagnostics: DiagnosticsResult) => {
    const suggestions: Array<{ type: 'error' | 'warning' | 'info'; text: string }> = [];
    
    // Add critical issues
    diagnostics.summary.critical_issues.forEach(issue => {
      suggestions.push({ type: 'error', text: issue });
    });
    
    // Add warnings
    diagnostics.summary.warnings.forEach(warning => {
      suggestions.push({ type: 'warning', text: warning });
    });
    
    // Add backend-specific suggestions
    const { checks } = diagnostics;
    
    if (diagnostics.backend === 'phaxio') {
      const phaxio = checks.phaxio || {};
      if (!phaxio.api_key_set) suggestions.push({ type: 'error', text: 'Set PHAXIO_API_KEY in .env' });
      if (!phaxio.api_secret_set) suggestions.push({ type: 'error', text: 'Set PHAXIO_API_SECRET in .env' });
      if (!phaxio.callback_url_set) suggestions.push({ type: 'warning', text: 'Set PHAXIO_STATUS_CALLBACK_URL (or PHAXIO_CALLBACK_URL)' });
      if (phaxio.public_url_https === false) suggestions.push({ type: 'warning', text: 'Use HTTPS for PUBLIC_API_URL' });
    }
    
    if (diagnostics.backend === 'sip') {
      const sip = checks.sip || {};
      if (sip.ami_password_not_default === false) suggestions.push({ type: 'error', text: 'Change ASTERISK_AMI_PASSWORD from default "changeme"' });
      if (sip.ami_reachable === false) suggestions.push({ type: 'error', text: 'Verify Asterisk AMI host/port/credentials and network reachability' });
    }
    
    const system = checks.system || {};
    if (system.ghostscript === false) suggestions.push({ type: 'warning', text: 'Install Ghostscript (gs) for PDF→TIFF conversion' });
    if (system.fax_data_writable === false) suggestions.push({ type: 'error', text: 'Ensure FAX_DATA_DIR exists and is writable' });
    if (system.database_connected === false) suggestions.push({ type: 'error', text: 'Fix DATABASE_URL connectivity' });
    
    return suggestions;
  };

  const getSuggestionIcon = (type: string) => {
    switch (type) {
      case 'error': return <ErrorIcon color="error" />;
      case 'warning': return <WarningIcon color="warning" />;
      default: return <CheckCircleIcon color="info" />;
    }
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          System Diagnostics
        </Typography>
        <Button
          variant="contained"
          startIcon={loading ? <CircularProgress size={20} /> : <PlayArrowIcon />}
          onClick={runDiagnostics}
          disabled={loading}
        >
          {loading ? 'Running...' : 'Run Diagnostics'}
        </Button>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      {diagnostics && (
        <Box>
          {/* Summary */}
          <Card sx={{ mb: 3 }}>
            <CardContent>
              <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
                <Typography variant="h6">
                  System Health Summary
                </Typography>
                <Chip
                  label={diagnostics.summary.healthy ? 'Healthy' : 'Issues Found'}
                  color={diagnostics.summary.healthy ? 'success' : 'error'}
                  variant="outlined"
                />
              </Box>
              <Typography variant="body2" color="text.secondary">
                Backend: {diagnostics.backend} • 
                Timestamp: {new Date(diagnostics.timestamp).toLocaleString()}
              </Typography>
            </CardContent>
          </Card>

          {/* Suggestions */}
          {(() => {
            const suggestions = getSuggestions(diagnostics);
            return suggestions.length > 0 && (
              <Card sx={{ mb: 3 }}>
                <CardContent>
                  <Typography variant="h6" gutterBottom>
                    Recommendations
                  </Typography>
                  <List dense>
                    {suggestions.map((suggestion, index) => (
                      <ListItem key={index}>
                        <ListItemIcon>
                          {getSuggestionIcon(suggestion.type)}
                        </ListItemIcon>
                        <ListItemText primary={suggestion.text} />
                      </ListItem>
                    ))}
                  </List>
                </CardContent>
              </Card>
            );
          })()}

          {/* Detailed Checks */}
          {diagnostics.checks.system && renderChecks(diagnostics.checks.system, 'System Checks')}
          {diagnostics.checks.phaxio && renderChecks(diagnostics.checks.phaxio, 'Phaxio Configuration')}
          {diagnostics.checks.sinch && renderChecks(diagnostics.checks.sinch, 'Sinch Configuration')}
          {diagnostics.checks.sip && renderChecks(diagnostics.checks.sip, 'SIP/Asterisk Configuration')}
          {diagnostics.checks.storage && renderChecks(diagnostics.checks.storage, 'Storage Configuration')}
          {diagnostics.checks.inbound && renderChecks(diagnostics.checks.inbound, 'Inbound Configuration')}
          {diagnostics.checks.security && renderChecks(diagnostics.checks.security, 'Security Configuration')}

          {/* Raw Results */}
          <Card>
            <CardContent>
              <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
                <Typography variant="h6">
                  Raw Diagnostics Data
                </Typography>
                <Box>
                  <Button
                    variant="outlined"
                    startIcon={<ContentCopyIcon />}
                    onClick={() => copyToClipboard(JSON.stringify(diagnostics, null, 2))}
                    sx={{ mr: 1 }}
                  >
                    Copy
                  </Button>
                  <Button
                    variant="outlined"
                    startIcon={<DownloadIcon />}
                    onClick={() => downloadText('diagnostics.json', JSON.stringify(diagnostics, null, 2))}
                  >
                    Download
                  </Button>
                </Box>
              </Box>
              
              <Paper sx={{ p: 2, bgcolor: 'background.default' }}>
                <pre style={{ 
                  margin: 0, 
                  fontSize: '0.875rem', 
                  overflow: 'auto',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word'
                }}>
                  {JSON.stringify(diagnostics, null, 2)}
                </pre>
              </Paper>
            </CardContent>
          </Card>
        </Box>
      )}

      {!diagnostics && !loading && (
        <Alert severity="info">
          Click "Run Diagnostics" to perform comprehensive system health checks including:
          <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
            <li>Backend connectivity and configuration</li>
            <li>Database and file system permissions</li>
            <li>Required system dependencies</li>
            <li>Security posture validation</li>
            <li>HIPAA compliance checks</li>
          </ul>
        </Alert>
      )}
    </Box>
  );
}

export default Diagnostics;
