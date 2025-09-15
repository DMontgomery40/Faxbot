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
import { Dialog, DialogTitle, DialogContent, DialogActions, Link } from '@mui/material';

interface DiagnosticsProps {
  client: AdminAPIClient;
  onNavigate?: (index: number) => void;
  docsBase?: string;
}

function Diagnostics({ client, onNavigate, docsBase }: DiagnosticsProps) {
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [helpTitle, setHelpTitle] = useState<string>('');
  const [helpKey, setHelpKey] = useState<string>('');
  const [testSending, setTestSending] = useState(false);
  const [testJobId, setTestJobId] = useState<string | null>(null);
  const [testStatus, setTestStatus] = useState<string | null>(null);

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

  const anchorFor = (title: string) => {
    const t = title.toLowerCase();
    if (t.includes('phaxio')) return '#settings-phaxio';
    if (t.includes('sinch')) return '#settings-backend';
    if (t.includes('sip')) return '#settings-sip';
    if (t.includes('storage')) return '#settings-storage';
    if (t.includes('security')) return '#settings-security';
    if (t.includes('inbound')) return '#settings-inbound';
    return '#settings-advanced';
  };

  const helpFor = (title: string, key: string, value: any): string | null => {
    const t = title.toLowerCase();
    if (t.includes('phaxio')) {
      if (key === 'api_key_set' && !value) return 'Set PHAXIO_API_KEY with your Phaxio console key.';
      if (key === 'api_secret_set' && !value) return 'Set PHAXIO_API_SECRET with your Phaxio console secret.';
      if (key === 'callback_url_set' && !value) return 'Set PHAXIO_STATUS_CALLBACK_URL so Phaxio can send status updates.';
      if (key === 'public_url_https' && !value) return 'PUBLIC_API_URL should be HTTPS for PHI; enable TLS.';
    }
    if (t.includes('sinch')) {
      if (key === 'project_id_set' && !value) return 'Set SINCH_PROJECT_ID from your Sinch console.';
      if (key === 'api_key_set' && !value) return 'Set SINCH_API_KEY (or PHAXIO_API_KEY) for Sinch.';
      if (key === 'api_secret_set' && !value) return 'Set SINCH_API_SECRET (or PHAXIO_API_SECRET) for Sinch.';
    }
    if (t.includes('sip')) {
      if (key === 'ami_password_not_default' && !value) return 'Change ASTERISK_AMI_PASSWORD from default to a secure value.';
      if (key === 'ami_reachable' && !value) return 'Verify AMI host/port, credentials, and network reachability.';
    }
    if (t.includes('storage')) {
      if (key === 'kms_enabled' && !value) return 'Set S3_KMS_KEY_ID to enable server-side encryption (KMS).';
      if (key === 'bucket_set' && !value) return 'Set S3_BUCKET to store inbound artifacts.';
    }
    if (t.includes('security')) {
      if (key === 'enforce_https' && !value) return 'Set ENFORCE_PUBLIC_HTTPS=true for HIPAA deployments.';
      if (key === 'audit_logging' && !value) return 'Enable AUDIT_LOG_ENABLED=true to record security events.';
      if (key === 'rate_limiting' && !value) return 'Set MAX_REQUESTS_PER_MINUTE to mitigate abuse.';
      if (key === 'pdf_token_ttl' && !value) return 'Set a reasonable PDF_TOKEN_TTL_MINUTES for Phaxio token links.';
    }
    if (t.includes('system')) {
      if (key === 'ghostscript' && !value) return 'Install ghostscript in production to support PDF→TIFF.';
      if (key === 'fax_data_writable' && !value) return 'Ensure FAX_DATA_DIR is writable (default /faxdata).';
      if (key === 'database_connected' && !value) return 'Check DATABASE_URL and ensure DB file or Postgres is reachable.';
    }
    if (t.includes('inbound')) {
      if (key === 'enabled' && !value) return 'Enable inbound to receive faxes.';
    }
    // Default lightweight hint so every row has help
    return 'See linked docs for configuration details.';
  };

  const getHelpDocs = (title: string, key: string) => {
    const t = title.toLowerCase();
    const docs: { text: string; href?: string }[] = [];
    if (t.includes('sip')) {
      if (key === 'ami_reachable') {
        docs.push({ text: 'Asterisk Manager Interface (AMI)', href: 'https://wiki.asterisk.org/wiki/display/AST/Asterisk+Manager+Interface+%28AMI%29' });
        docs.push({ text: 'Verify docker compose asterisk service is running and reachable as host "asterisk" on port 5038.' });
        docs.push({ text: 'Ensure ASTERISK_AMI_USERNAME/PASSWORD match Asterisk manager.conf and that port 5038 is not exposed publicly.' });
      }
      if (key === 'ami_password_not_default') {
        docs.push({ text: 'Change ASTERISK_AMI_PASSWORD from default to a secure value in both Faxbot and manager.conf.' });
      }
    }
    if (t.includes('phaxio')) {
      docs.push({ text: 'Faxbot: Phaxio setup', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/phaxio-setup.html` });
      docs.push({ text: 'Sinch Fax (Phaxio) API', href: 'https://developers.sinch.com/docs/fax/' });
      docs.push({ text: 'Webhook signature (HMAC)', href: 'https://developers.sinch.com/docs/fax/' });
    }
    if (t.includes('sinch')) {
      docs.push({ text: 'Sinch Fax API reference', href: 'https://developers.sinch.com/docs/fax/' });
      docs.push({ text: 'Faxbot inbound security can also enforce optional Basic/HMAC on callbacks. Configure shared secrets in Faxbot and, if supported, in your provider portal.' });
    }
    if (t.includes('storage')) {
      docs.push({ text: 'S3 server-side encryption with KMS (AWS docs)', href: 'https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html' });
    }
    if (t.includes('security')) {
      docs.push({ text: 'Enforce HTTPS (ENFORCE_PUBLIC_HTTPS) and enable audit logging for HIPAA.' });
    }
    if (t.includes('system')) {
      docs.push({ text: 'Ghostscript install (docs)', href: 'https://ghostscript.readthedocs.io/' });
    }
    return docs;
  };

  const renderChecks = (checks: Record<string, any>, title: string) => {
    // Respect active backend: show N/A for inactive providers in demo
    const active = diagnostics?.backend || 'phaxio';
    const isPhax = title.toLowerCase().includes('phaxio');
    const isSinch = title.toLowerCase().includes('sinch');
    const isSip = title.toLowerCase().includes('sip');
    if ((isPhax && active !== 'phaxio') || (isSinch && active !== 'sinch') || (isSip && active !== 'sip')) {
      return (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              {title}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              N/A — inactive backend
            </Typography>
          </CardContent>
        </Card>
      );
    }
    return (
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            {title}
          </Typography>
          {Object.entries(checks).map(([key, value]) => {
            const help = helpFor(title, key, value);
            const label = (typeof value === 'boolean') ? (value ? 'View' : 'Help') : 'Help';
            return (
              <Box key={key} display="flex" justifyContent="space-between" alignItems="center" mb={1}>
                <Box>
                  <Typography variant="body2">{key.replace(/_/g, ' ')}:</Typography>
                  <Typography variant="caption" color="text.secondary">{help}</Typography>
                </Box>
                <Box display="flex" alignItems="center" gap={1}>
                  {renderCheckValue(value)}
                  <Button size="small" variant="outlined" onClick={() => { setHelpTitle(title); setHelpKey(key); setHelpOpen(true); }}>{label}</Button>
                  <Button size="small" onClick={() => { if (onNavigate) { const a = anchorFor(title); window.location.hash = a; onNavigate(6); } }}>Open Settings</Button>
                </Box>
              </Box>
            );
          })}
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
    // Only suggest SIP issues when SIP is active
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

  const runSendTestFax = async () => {
    try {
      setError(null);
      setTestSending(true);
      setTestJobId(null);
      setTestStatus(null);
      // Create a tiny text file in memory
      const blob = new Blob(["Faxbot test"], { type: 'text/plain' });
      const file = new File([blob], 'test.txt', { type: 'text/plain' });
      const result = await client.sendFax('+15555550123', file);
      setTestJobId(result.id);
      setTestStatus(result.status);
      // Poll status a few times
      let attempts = 0;
      const poll = async () => {
        if (!result.id || attempts++ > 10) return; // ~10 polls
        try {
          const job = await client.getJob(result.id);
          setTestStatus(job.status);
          if (['SUCCESS','FAILED','failed','SUCCESSFUL','COMPLETED'].includes(String(job.status))) return;
          // Show matching log snippet if available
          try {
            const logs = await client.getLogs({ q: result.id, limit: 5 });
            if (logs.items && logs.items.length > 0) {
              // Put latest log JSON in error banner if failure
              // (lightweight: do nothing here but could display inline in future)
            }
          } catch {}
        } catch {}
        setTimeout(poll, 2000);
      };
      poll();
    } catch (e: any) {
      setError(e?.message || 'Test fax failed to start');
    } finally {
      setTestSending(false);
    }
  };

  return (
    <>
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          System Diagnostics
        </Typography>
        <Box display="flex" gap={1}>
          <Button
            variant="contained"
            startIcon={loading ? <CircularProgress size={20} /> : <PlayArrowIcon />}
            onClick={runDiagnostics}
            disabled={loading}
          >
            {loading ? 'Running...' : 'Run Diagnostics'}
          </Button>
          <Button
            variant="outlined"
            onClick={async () => { try { await client.restart(); } catch { /* ignore */ } }}
          >
            Restart API
          </Button>
        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      {diagnostics && (
        <Box>
          {/* Built-in Tests */}
          <Card sx={{ mb: 3 }}>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Built‑in Tests
              </Typography>
              <Box display="flex" alignItems="center" gap={1}>
                <Button variant="outlined" onClick={runSendTestFax} disabled={testSending}>
                  {testSending ? 'Sending…' : 'Send Test Fax'}
                </Button>
                {testJobId && (
                  <Typography variant="body2" color="text.secondary">
                    Job: {testJobId} • Status: {testStatus || 'queued'}
                  </Typography>
                )}
              </Box>
              <Typography variant="caption" color="text.secondary">
                Uses your current backend settings. For cloud backends without valid credentials this will fail fast with an error.
              </Typography>
            </CardContent>
          </Card>
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
    <Dialog open={helpOpen} onClose={() => setHelpOpen(false)} maxWidth="md" fullWidth>
      <DialogTitle>Help — {helpTitle} / {helpKey.replace(/_/g,' ')}</DialogTitle>
      <DialogContent>
        {getHelpDocs(helpTitle, helpKey).map((d, i) => (
          <Typography key={i} variant="body2" sx={{ mb: 1 }}>
            {d.href ? (<Link href={d.href} target="_blank" rel="noreferrer">{d.text}</Link>) : d.text}
          </Typography>
        ))}
        {!getHelpDocs(helpTitle, helpKey).length && (
          <Typography variant="body2">No additional guidance available.</Typography>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={() => setHelpOpen(false)}>Close</Button>
      </DialogActions>
    </Dialog>
    </>
  );
}

export default Diagnostics;
