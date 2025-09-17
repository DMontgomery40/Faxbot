import { useState } from 'react';
import {
  Box,
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
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Link,
  Stack,
  useTheme,
  useMediaQuery,
  Fade,
  IconButton,
  Tooltip,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  LinearProgress,
} from '@mui/material';
import {
  Download as DownloadIcon,
  ContentCopy as ContentCopyIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  Warning as WarningIcon,
  Info as InfoIcon,
  RestartAlt as RestartIcon,
  ExpandMore as ExpandMoreIcon,
  Help as HelpIcon,
  Send as SendIcon,
  HealthAndSafety as HealthIcon,
  BugReport as DiagnosticIcon,
  Assessment as AssessmentIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { DiagnosticsResult } from '../api/types';
import { ResponsiveFormSection } from './common/ResponsiveFormFields';

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
  const [expandedSections, setExpandedSections] = useState<string[]>(['summary']);

  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

  const runDiagnostics = async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await client.runDiagnostics();
      setDiagnostics(data);
      setExpandedSections(['summary', ...Object.keys(data.checks || {})]);
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

  const handleSectionToggle = (section: string) => {
    setExpandedSections(prev => 
      prev.includes(section) 
        ? prev.filter(s => s !== section)
        : [...prev, section]
    );
  };

  const renderCheckValue = (value: any) => {
    if (typeof value === 'boolean') {
      return (
        <Chip
          icon={value ? <CheckCircleIcon /> : <ErrorIcon />}
          label={value ? 'Pass' : 'Fail'}
          color={value ? 'success' : 'error'}
          size="small"
          variant="outlined"
          sx={{ borderRadius: 1 }}
        />
      );
    }
    return <Typography variant="body2">{String(value)}</Typography>;
  };

  const anchorFor = (title: string) => {
    const t = title.toLowerCase();
    if (t.includes('phaxio')) return '#settings-phaxio';
    if (t.includes('sinch')) return '#settings-sinch';
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
    return 'See linked docs for configuration details.';
  };

  const getHelpDocs = (title: string, key: string) => {
    const t = title.toLowerCase();
    const docs: { text: string; href?: string }[] = [];
    
    if (t.includes('system')) {
      if (key === 'ghostscript') {
        docs.push({ text: 'Ghostscript is required for PDF to TIFF conversion (SIP/Asterisk backend).' });
        docs.push({ text: 'Ghostscript Documentation', href: 'https://ghostscript.readthedocs.io/' });
        docs.push({ text: 'Install via: apt-get install ghostscript (Linux) or brew install ghostscript (Mac)' });
      }
      else if (key === 'fax_data_dir' || key === 'fax_data_writable') {
        docs.push({ text: 'FAX_DATA_DIR stores temporary files and fax artifacts.' });
        docs.push({ text: 'Default: /faxdata in container, ./faxdata locally' });
        docs.push({ text: 'Must be writable by the application process.' });
        docs.push({ text: 'Deployment Guide', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/deployment/` });
      }
      else if (key === 'database_connected') {
        docs.push({ text: 'Database stores job records and API keys.' });
        docs.push({ text: 'Default: SQLite at ./faxbot.db' });
        docs.push({ text: 'Production: Use PostgreSQL with DATABASE_URL' });
        docs.push({ text: 'Database Setup', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/deployment/#database-configuration` });
      }
    }
    
    if (t.includes('phaxio')) {
      docs.push({ text: 'Phaxio Setup Guide', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/phaxio-setup.html` });
      docs.push({ text: 'Phaxio Console', href: 'https://console.phaxio.com' });
      if (key === 'public_url_https' || key === 'callback_url_set') {
        docs.push({ text: 'Webhook security requires HTTPS for PHI transmission.' });
      }
    }
    
    if (t.includes('sip')) {
      docs.push({ text: 'SIP/Asterisk Setup', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/backends/sip-setup.html` });
      if (key === 'ami_password_not_default') {
        docs.push({ text: 'Change AMI password in both Asterisk manager.conf and ASTERISK_AMI_PASSWORD env var.' });
      }
    }
    
    if (t.includes('security')) {
      docs.push({ text: 'Security Guide', href: `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/security/` });
      if (key === 'enforce_https') {
        docs.push({ text: 'HIPAA requires encryption in transit. Enable ENFORCE_PUBLIC_HTTPS=true.' });
      }
    }
    
    return docs;
  };

  const renderCheckSection = (title: string, checks: Record<string, any>) => {
    const sectionKey = title.toLowerCase().replace(/\s+/g, '_');
    const isExpanded = expandedSections.includes(sectionKey);
    
    const failCount = Object.values(checks).filter(v => v === false).length;
    const totalCount = Object.keys(checks).length;
    const hasIssues = failCount > 0;
    
    return (
      <Accordion 
        key={sectionKey}
        expanded={isExpanded}
        onChange={() => handleSectionToggle(sectionKey)}
        sx={{ 
          borderRadius: 2,
          mb: 2,
          '&:before': { display: 'none' },
          border: '1px solid',
          borderColor: 'divider',
        }}
      >
        <AccordionSummary
          expandIcon={<ExpandMoreIcon />}
          sx={{
            '& .MuiAccordionSummary-content': {
              alignItems: 'center',
              gap: 2,
            }
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, width: '100%' }}>
            {hasIssues ? <ErrorIcon color="error" /> : <CheckCircleIcon color="success" />}
            <Typography variant="h6" fontWeight={600}>
              {title}
            </Typography>
            <Chip
              label={`${totalCount - failCount}/${totalCount} Pass`}
              color={hasIssues ? 'error' : 'success'}
              size="small"
              variant="outlined"
              sx={{ borderRadius: 1, ml: 'auto' }}
            />
          </Box>
        </AccordionSummary>
        <AccordionDetails>
          <Stack spacing={2}>
            {Object.entries(checks).map(([key, value]) => {
              const help = helpFor(title, key, value);
              const displayKey = key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
              
              return (
                <Paper
                  key={key}
                  elevation={0}
                  sx={{
                    p: 2,
                    border: '1px solid',
                    borderColor: value === false ? 'error.main' : 'divider',
                    borderRadius: 2,
                    backgroundColor: theme.palette.mode === 'dark' 
                      ? 'rgba(255, 255, 255, 0.02)' 
                      : 'rgba(0, 0, 0, 0.02)',
                  }}
                >
                  <Box sx={{ 
                    display: 'flex', 
                    alignItems: 'flex-start',
                    flexDirection: isMobile ? 'column' : 'row',
                    gap: isMobile ? 1 : 2
                  }}>
                    <Box sx={{ flex: 1 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
                        <Typography variant="subtitle2" fontWeight={600}>
                          {displayKey}
                        </Typography>
                        {renderCheckValue(value)}
                      </Box>
                      {help && (
                        <Typography variant="caption" color="text.secondary">
                          {help}
                        </Typography>
                      )}
                    </Box>
                    <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                      {onNavigate && (
                        <Button
                          size="small"
                          variant="outlined"
                          onClick={() => {
                            const anchor = anchorFor(title);
                            onNavigate(1);
                            setTimeout(() => {
                              const el = document.querySelector(anchor);
                              el?.scrollIntoView({ behavior: 'smooth' });
                            }, 200);
                          }}
                          sx={{ borderRadius: 1 }}
                        >
                          Go to Settings
                        </Button>
                      )}
                      <Tooltip title="Get help">
                        <IconButton
                          size="small"
                          onClick={() => {
                            setHelpTitle(title);
                            setHelpKey(key);
                            setHelpOpen(true);
                          }}
                        >
                          <HelpIcon />
                        </IconButton>
                      </Tooltip>
                    </Box>
                  </Box>
                </Paper>
              );
            })}
          </Stack>
        </AccordionDetails>
      </Accordion>
    );
  };

  const getSuggestions = (diagnostics: DiagnosticsResult) => {
    const suggestions: Array<{ type: 'error' | 'warning' | 'info'; text: string }> = [];
    
    diagnostics.summary.critical_issues.forEach(issue => {
      suggestions.push({ type: 'error', text: issue });
    });
    
    diagnostics.summary.warnings.forEach(warning => {
      suggestions.push({ type: 'warning', text: warning });
    });
    
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
      default: return <InfoIcon color="info" />;
    }
  };

  const runSendTestFax = async () => {
    try {
      setError(null);
      setTestSending(true);
      setTestJobId(null);
      setTestStatus(null);
      const blob = new Blob(["Faxbot test"], { type: 'text/plain' });
      const file = new File([blob], 'test.txt', { type: 'text/plain' });
      const result = await client.sendFax('+15555550123', file);
      setTestJobId(result.id);
      setTestStatus(result.status);
      
      let attempts = 0;
      const poll = async () => {
        if (!result.id || attempts++ > 10) return;
        try {
          const job = await client.getJob(result.id);
          setTestStatus(job.status);
          if (['SUCCESS','FAILED','failed','SUCCESSFUL','COMPLETED'].includes(String(job.status))) return;
          try {
            const logs = await client.getLogs({ q: result.id, limit: 5 });
            if (logs.items && logs.items.length > 0) {
              // Could display logs inline in future
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
        <Box 
          display="flex" 
          justifyContent="space-between" 
          alignItems={{ xs: 'flex-start', sm: 'center' }}
          flexDirection={{ xs: 'column', sm: 'row' }}
          gap={2}
          mb={3}
        >
          <Typography variant="h4" component="h1">
            System Diagnostics
          </Typography>
          <Box display="flex" gap={1}>
            <Button
              variant="contained"
              startIcon={loading ? <CircularProgress size={20} color="inherit" /> : <DiagnosticIcon />}
              onClick={runDiagnostics}
              disabled={loading}
              size={isSmallMobile ? 'medium' : 'large'}
              sx={{ 
                borderRadius: 2,
                minHeight: isSmallMobile ? 40 : 42,
              }}
            >
              {loading ? 'Running...' : 'Run Diagnostics'}
            </Button>
            <Button
              variant="outlined"
              startIcon={<RestartIcon />}
              onClick={async () => { try { await client.restart(); } catch { /* ignore */ } }}
              size={isSmallMobile ? 'medium' : 'large'}
              sx={{ 
                borderRadius: 2,
                minHeight: isSmallMobile ? 40 : 42,
              }}
            >
              {isSmallMobile ? 'Restart' : 'Restart API'}
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

        {!diagnostics && !loading && (
          <Fade in>
            <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 2 }}>
              <HealthIcon sx={{ fontSize: 48, color: 'text.secondary', mb: 2 }} />
              <Typography variant="h6" gutterBottom>
                Run System Diagnostics
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
                Check your Faxbot configuration, backend connectivity, and system health
              </Typography>
              <Button
                variant="contained"
                startIcon={<DiagnosticIcon />}
                onClick={runDiagnostics}
                sx={{ borderRadius: 2 }}
              >
                Start Diagnostics
              </Button>
            </Paper>
          </Fade>
        )}

        {loading && (
          <Fade in>
            <Paper sx={{ p: 4, borderRadius: 2 }}>
              <Box sx={{ textAlign: 'center' }}>
                <CircularProgress sx={{ mb: 2 }} />
                <Typography variant="body1">
                  Running diagnostics...
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  Checking system health, backend connectivity, and configuration
                </Typography>
              </Box>
              <LinearProgress sx={{ mt: 3 }} />
            </Paper>
          </Fade>
        )}

        {diagnostics && (
          <Fade in>
            <Box>
              {/* Built-in Tests */}
              <ResponsiveFormSection
                title="Built-in Tests"
                subtitle="Test your fax configuration with a sample transmission"
                icon={<SendIcon />}
              >
                <Stack spacing={2}>
                  <Box sx={{ 
                    display: 'flex', 
                    alignItems: 'center', 
                    gap: 2,
                    flexWrap: 'wrap'
                  }}>
                    <Button 
                      variant="outlined" 
                      onClick={runSendTestFax} 
                      disabled={testSending}
                      startIcon={testSending ? <CircularProgress size={16} /> : <SendIcon />}
                      sx={{ borderRadius: 2 }}
                    >
                      {testSending ? 'Sending…' : 'Send Test Fax'}
                    </Button>
                    {testJobId && (
                      <Chip
                        icon={<AssessmentIcon />}
                        label={`Job: ${testJobId.slice(0, 8)}... • Status: ${testStatus || 'queued'}`}
                        variant="outlined"
                        sx={{ borderRadius: 1 }}
                      />
                    )}
                  </Box>
                  <Typography variant="caption" color="text.secondary">
                    Uses your current backend settings. For cloud backends without valid credentials this will fail fast with an error.
                  </Typography>
                </Stack>
              </ResponsiveFormSection>

              {/* Summary */}
              <ResponsiveFormSection
                title="Diagnostic Summary"
                subtitle={`Backend: ${diagnostics.backend} • Health: ${diagnostics.summary.healthy ? 'Healthy' : 'Issues Detected'}`}
                icon={<HealthIcon />}
              >
                <Stack spacing={3}>
                  <Box>
                    <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>
                      Overall Health
                    </Typography>
                    <Chip
                      icon={diagnostics.summary.healthy ? <CheckCircleIcon /> : <ErrorIcon />}
                      label={diagnostics.summary.healthy ? 'Healthy' : 'Issues Detected'}
                      color={diagnostics.summary.healthy ? 'success' : 'error'}
                      sx={{ borderRadius: 1 }}
                    />
                  </Box>

                  {getSuggestions(diagnostics).length > 0 && (
                    <Box>
                      <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>
                        Issues & Suggestions
                      </Typography>
                      <List dense>
                        {getSuggestions(diagnostics).map((suggestion, idx) => (
                          <ListItem key={idx} sx={{ px: 0 }}>
                            <ListItemIcon sx={{ minWidth: 36 }}>
                              {getSuggestionIcon(suggestion.type)}
                            </ListItemIcon>
                            <ListItemText 
                              primary={suggestion.text}
                              primaryTypographyProps={{ 
                                variant: 'body2',
                                color: suggestion.type === 'error' ? 'error' : 'text.primary'
                              }}
                            />
                          </ListItem>
                        ))}
                      </List>
                    </Box>
                  )}

                  <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                    <Button
                      variant="outlined"
                      startIcon={<ContentCopyIcon />}
                      onClick={() => copyToClipboard(JSON.stringify(diagnostics, null, 2))}
                      size="small"
                      sx={{ borderRadius: 1 }}
                    >
                      Copy JSON
                    </Button>
                    <Button
                      variant="outlined"
                      startIcon={<DownloadIcon />}
                      onClick={() => downloadText('diagnostics.json', JSON.stringify(diagnostics, null, 2))}
                      size="small"
                      sx={{ borderRadius: 1 }}
                    >
                      Download
                    </Button>
                  </Box>
                </Stack>
              </ResponsiveFormSection>

              {/* Check Sections */}
              <Box sx={{ mt: 3 }}>
                <Typography variant="h6" fontWeight={600} sx={{ mb: 2 }}>
                  System Checks
                </Typography>
                {Object.entries(diagnostics.checks).map(([title, checks]) => (
                  renderCheckSection(title.charAt(0).toUpperCase() + title.slice(1), checks as Record<string, any>)
                ))}
              </Box>
            </Box>
          </Fade>
        )}
      </Box>

      {/* Help Dialog */}
      <Dialog 
        open={helpOpen} 
        onClose={() => setHelpOpen(false)}
        maxWidth="sm"
        fullWidth
        fullScreen={isSmallMobile}
      >
        <DialogTitle>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <HelpIcon />
            Help: {helpTitle} - {helpKey.replace(/_/g, ' ')}
          </Box>
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2}>
            {getHelpDocs(helpTitle, helpKey).map((doc, idx) => (
              <Box key={idx}>
                {doc.href ? (
                  <Link href={doc.href} target="_blank" rel="noreferrer">
                    {doc.text}
                  </Link>
                ) : (
                  <Typography variant="body2">{doc.text}</Typography>
                )}
              </Box>
            ))}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setHelpOpen(false)} sx={{ borderRadius: 2 }}>
            Close
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}

export default Diagnostics;