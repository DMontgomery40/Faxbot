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
    
    // System Checks - specific help for each check
    if (t.includes('system')) {
      if (key === 'ghostscript') {
        docs.push({ text: 'Ghostscript is required for PDF to TIFF conversion (SIP/Asterisk backend).' });
        docs.push({ text: 'Ghostscript Documentation', href: 'https://ghostscript.readthedocs.io/' });
        docs.push({ text: 'Install via: apt-get install ghostscript (Linux) or brew install ghostscript (Mac)' });
      }
      else if (key === 'fax_data_dir' || key === 'fax_data_writable') {
        docs.push({ text: 'The fax data directory stores temporary and permanent fax files.' });
        docs.push({ text: 'Default location: /faxdata (in Docker) or ./faxdata (local)' });
        docs.push({ text: 'Set FAX_DATA_DIR environment variable to customize location.' });
        docs.push({ text: 'Ensure the directory has write permissions for the API process.' });
      }
      else if (key === 'temp_dir_writable') {
        docs.push({ text: 'Temporary directory is used for processing files during transmission.' });
        docs.push({ text: 'Default: system temp directory (/tmp or %TEMP%)' });
        docs.push({ text: 'Ensure adequate space and write permissions.' });
      }
      else if (key === 'database_connected') {
        docs.push({ text: 'Database stores job status and metadata.' });
        docs.push({ text: 'Default: SQLite (faxbot.db)' });
        docs.push({ text: 'For production: Use PostgreSQL with DATABASE_URL environment variable.' });
        docs.push({ text: 'Faxbot Database Guide', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/deployment.html#database` });
      }
    }
    
    // Phaxio Configuration
    else if (t.includes('phaxio')) {
      if (key === 'api_key_set' || key === 'api_secret_set') {
        docs.push({ text: 'Get your API credentials from the Phaxio Console', href: 'https://console.phaxio.com/api_credentials' });
        docs.push({ text: 'Set PHAXIO_API_KEY and PHAXIO_API_SECRET in your .env file' });
        docs.push({ text: 'Never commit API keys to version control' });
      }
      else if (key === 'callback_url_set') {
        docs.push({ text: 'Webhooks receive status updates from Phaxio', href: 'https://developers.sinch.com/docs/fax/getting-started/webhooks/' });
        docs.push({ text: 'Set PHAXIO_CALLBACK_URL to your public endpoint (https://yourdomain.com/phaxio-callback)' });
        docs.push({ text: 'Must be publicly accessible with valid HTTPS certificate' });
      }
      else if (key === 'public_url_https') {
        docs.push({ text: 'HIPAA requires HTTPS for all PHI transmission' });
        docs.push({ text: 'Set PUBLIC_API_URL with https:// prefix' });
        docs.push({ text: 'Use Let\'s Encrypt or other valid SSL certificate' });
      }
      else if (key === 'verify_signature') {
        docs.push({ text: 'HMAC signature verification prevents webhook spoofing' });
        docs.push({ text: 'Set PHAXIO_VERIFY_SIGNATURE=true for production' });
        docs.push({ text: 'Webhook Security Guide', href: 'https://developers.sinch.com/docs/fax/getting-started/webhooks/#verifying-webhook-signatures' });
      }
      else {
        docs.push({ text: 'Complete Phaxio Setup Guide', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/phaxio-setup.html` });
        docs.push({ text: 'Phaxio Developer Documentation', href: 'https://developers.sinch.com/docs/fax/' });
      }
    }
    
    // Sinch Configuration
    else if (t.includes('sinch')) {
      if (key === 'project_id_set') {
        docs.push({ text: 'Find your Project ID in Sinch Dashboard', href: 'https://dashboard.sinch.com' });
        docs.push({ text: 'Set SINCH_PROJECT_ID in your .env file' });
      }
      else if (key === 'api_key_set' || key === 'api_secret_set') {
        docs.push({ text: 'Create API credentials in Sinch Dashboard', href: 'https://dashboard.sinch.com/settings/access-keys' });
        docs.push({ text: 'Set SINCH_API_KEY and SINCH_API_SECRET (or reuse PHAXIO_* if compatible)' });
      }
      else {
        docs.push({ text: 'Sinch Fax API Documentation', href: 'https://developers.sinch.com/docs/fax/' });
        docs.push({ text: 'Faxbot Sinch Setup', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/sinch-setup.html` });
      }
    }
    
    // SIP/Asterisk Configuration
    else if (t.includes('sip')) {
      if (key === 'ami_reachable') {
        docs.push({ text: 'Asterisk Manager Interface (AMI) Documentation', href: 'https://wiki.asterisk.org/wiki/display/AST/Asterisk+Manager+Interface+%28AMI%29' });
        docs.push({ text: 'Ensure Asterisk container is running: docker compose up -d asterisk' });
        docs.push({ text: 'Default connection: host=asterisk, port=5038' });
        docs.push({ text: 'Check firewall rules and Docker networking' });
      }
      else if (key === 'ami_password_not_default') {
        docs.push({ text: 'Change ASTERISK_AMI_PASSWORD from "changeme" to a secure value' });
        docs.push({ text: 'Update both .env and asterisk/manager.conf' });
        docs.push({ text: 'Restart Asterisk after changing: docker compose restart asterisk' });
      }
      else {
        docs.push({ text: 'Complete SIP/Asterisk Setup', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/sip-setup.html` });
        docs.push({ text: 'T.38 Fax Protocol Guide', href: 'https://www.voip-info.org/t-38/' });
      }
    }
    
    // Storage Configuration
    else if (t.includes('storage')) {
      if (key === 'backend') {
        docs.push({ text: 'Storage backends: local (dev) or s3 (production)' });
        docs.push({ text: 'Set STORAGE_BACKEND=s3 for production with S3 or compatible storage' });
      }
      else if (key === 'bucket_set') {
        docs.push({ text: 'Create an S3 bucket for inbound fax storage' });
        docs.push({ text: 'Set S3_BUCKET environment variable' });
        docs.push({ text: 'S3 Bucket Creation Guide', href: 'https://docs.aws.amazon.com/AmazonS3/latest/userguide/creating-bucket.html' });
      }
      else if (key === 'kms_enabled') {
        docs.push({ text: 'KMS encryption protects PHI at rest' });
        docs.push({ text: 'Set S3_KMS_KEY_ID to enable server-side encryption' });
        docs.push({ text: 'AWS KMS Documentation', href: 'https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html' });
      }
      else {
        docs.push({ text: 'Storage Configuration Guide', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/deployment.html#storage` });
      }
    }
    
    // Security Configuration
    else if (t.includes('security')) {
      if (key === 'api_key_required') {
        docs.push({ text: 'API key authentication protects endpoints' });
        docs.push({ text: 'Set REQUIRE_API_KEY=true for production' });
        docs.push({ text: 'Generate keys via Admin Console or set API_KEY environment variable' });
      }
      else if (key === 'enforce_https') {
        docs.push({ text: 'HTTPS is required for HIPAA compliance' });
        docs.push({ text: 'Set ENFORCE_PUBLIC_HTTPS=true for production' });
        docs.push({ text: 'Deploy behind reverse proxy with valid SSL certificate' });
      }
      else if (key === 'audit_logging') {
        docs.push({ text: 'Audit logs track all PHI access and system events' });
        docs.push({ text: 'Set AUDIT_LOG_ENABLED=true and AUDIT_LOG_FILE=/path/to/audit.log' });
        docs.push({ text: 'HIPAA Compliance Guide', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/security.html#hipaa` });
      }
      else if (key === 'rate_limiting') {
        docs.push({ text: 'Rate limiting prevents abuse and DoS attacks' });
        docs.push({ text: 'Set MAX_REQUESTS_PER_MINUTE (default: 60)' });
        docs.push({ text: 'Configure per-key limits in Admin Console' });
      }
      else if (key === 'pdf_token_ttl') {
        docs.push({ text: 'PDF tokens provide time-limited access to transmitted documents' });
        docs.push({ text: 'Set PDF_TOKEN_TTL_MINUTES (default: 60)' });
        docs.push({ text: 'Shorter TTLs improve security but may inconvenience users' });
      }
      else {
        docs.push({ text: 'Security Best Practices', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/security.html` });
      }
    }
    
    // Inbound Configuration
    else if (t.includes('inbound')) {
      if (key === 'enabled') {
        docs.push({ text: 'Enable inbound fax receiving with INBOUND_ENABLED=true' });
        docs.push({ text: 'Requires storage backend configuration' });
        docs.push({ text: 'Inbound Setup Guide', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/inbound.html` });
      }
      else if (key === 'retention_days') {
        docs.push({ text: 'Set INBOUND_RETENTION_DAYS to control storage duration' });
        docs.push({ text: 'Default: 30 days' });
        docs.push({ text: 'Consider compliance requirements for your industry' });
      }
      else {
        docs.push({ text: 'Inbound Fax Documentation', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/inbound.html` });
      }
    }
    
    // Fallback for any uncovered cases
    if (docs.length === 0) {
      docs.push({ text: 'Complete Faxbot Documentation', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}` });
      docs.push({ text: 'For additional help, check the troubleshooting guide or file an issue on GitHub.' });
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
          {diagnostics.checks.plugins && (
            <Card sx={{ mb: 3 }}>
              <CardContent>
                <Typography variant="h6" gutterBottom>Plugins (v3)</Typography>
                <Box display="flex" gap={1} alignItems="center" sx={{ mb: 1 }}>
                  <Chip size="small" label={diagnostics.checks.plugins.v3_enabled ? 'Enabled' : 'Disabled'} color={diagnostics.checks.plugins.v3_enabled ? 'success' : 'default'} variant="outlined" />
                  <Chip size="small" label={`Installed: ${diagnostics.checks.plugins.installed || 0}`} variant="outlined" />
                  <Chip size="small" label={`Active outbound: ${diagnostics.checks.plugins.active_outbound || '-'}`} variant="outlined" />
                </Box>
                {(diagnostics.checks.plugins.manifests || []).length === 0 ? (
                  <Typography variant="body2" color="text.secondary">No manifest providers installed.</Typography>
                ) : (
                  <List dense>
                    {(diagnostics.checks.plugins.manifests || []).map((m: any) => (
                      <ListItem key={m.id} alignItems="flex-start">
                        <ListItemIcon>{(m.issues && m.issues.length>0) ? <WarningIcon color="warning" /> : <CheckCircleIcon color="success" />}</ListItemIcon>
                        <ListItemText
                          primary={`${m.name || m.id}`}
                          secondary={
                            <Box>
                              <Typography variant="caption" color="text.secondary">Actions: {(m.actions || []).join(', ') || '—'}</Typography>
                              <br />
                              <Typography variant="caption" color="text.secondary">Allowed domains: {(m.allowed_domains || []).join(', ') || '—'}</Typography>
                              {(m.issues || []).length > 0 && (
                                <Box sx={{ mt: 0.5 }}>
                                  {(m.issues || []).map((iss: string, idx: number) => (
                                    <Chip key={idx} size="small" color="warning" label={iss} sx={{ mr: 0.5, mb: 0.5 }} />
                                  ))}
                                </Box>
                              )}
                            </Box>
                          }
                        />
                      </ListItem>
                    ))}
                  </List>
                )}
                <Typography variant="caption" color="text.secondary">Edit manifests from Plugins → HTTP Manifest Tester (preview) or install manifests into config/providers/&lt;id&gt;/manifest.json.</Typography>
              </CardContent>
            </Card>
          )}

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
