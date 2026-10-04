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
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import { docsLink } from '../docsLinks';
import type { DiagnosticsOutcome, DiagnosticsResult, DiagnosticsValue } from '../api/types';
import { formatServerTime } from '../api/time';
import type { AdminDestination } from '../navigation';
import { ResponsiveFormSection } from './common/ResponsiveFormFields';
import DatabaseStatus from './DatabaseStatus';
import { providerLabel } from '../providerLabels';

interface DiagnosticsProps {
  client: AdminAPIClient;
  onNavigate?: (destination: AdminDestination) => void;
  docsBase?: string;
}

type ActionNotice = { severity: 'success' | 'error' | 'info'; text: string };

function explicitOutcome(value: unknown): DiagnosticsOutcome {
  switch (value) {
    case 'pass':
    case 'fail':
    case 'warning':
    case 'not_applicable':
      return value;
    default:
      return 'info';
  }
}

function OutcomeChip({ outcome }: { outcome: DiagnosticsOutcome }) {
  const label = outcome === 'not_applicable' ? 'Not applicable' : outcome.charAt(0).toUpperCase() + outcome.slice(1);
  const color = outcome === 'pass' ? 'success' : outcome === 'fail' ? 'error' : outcome === 'warning' ? 'warning' : 'default';
  const icon = outcome === 'pass' ? <CheckCircleIcon /> : outcome === 'fail' ? <ErrorIcon /> : outcome === 'warning' ? <WarningIcon /> : <InfoIcon />;
  return <Chip icon={icon} label={label} color={color} size="small" variant="outlined" sx={{ borderRadius: 1 }} />;
}

// Plain sentences for fixed server notes that use internal words.
const PLAIN_NOTES: Record<string, string> = {
  'Desired settings are pending a full installation restart; diagnostics describe the active revision.':
    'Some saved settings take effect after Faxbot restarts; these results describe the settings in use now.',
};

// What Copy and Download hand out: configuration as plain facts, without
// internal revision identifiers or counters.
export function diagnosticsForExport(result: DiagnosticsResult) {
  return {
    ...result,
    configuration: { pending_restart: result.configuration.pending_restart },
    summary: { ...result.summary, warnings: result.summary.warnings.map((text) => PLAIN_NOTES[text] ?? text) },
  };
}

function plainValue(value: DiagnosticsValue): string {
  if (value === null) return 'Not set';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return value.toLocaleString();
  if (typeof value === 'string') return value;
  if (Array.isArray(value) && value.every((item) => typeof item === 'string' || typeof item === 'number')) {
    return value.length ? value.join(', ') : 'None';
  }
  return JSON.stringify(value, null, 2);
}

function CheckValue({ value }: { value: DiagnosticsValue }) {
  let text: string;
  try {
    text = plainValue(value);
  } catch {
    text = 'Value could not be displayed.';
  }
  const limit = 8000;
  const truncated = text.length > limit;
  return (
    <Box sx={{ minWidth: 0, mt: 1 }}>
      <Box
        component="pre"
        sx={{
          m: 0,
          fontFamily: 'monospace',
          fontSize: '0.8125rem',
          whiteSpace: 'pre-wrap',
          overflowWrap: 'anywhere',
          maxHeight: 240,
          overflow: 'auto',
        }}
      >
        {truncated ? text.slice(0, limit) : text}
      </Box>
      {truncated && (
        <Typography variant="caption" color="text.secondary">
          Display limited to {limit.toLocaleString()} characters. Copy or download JSON for the full value.
        </Typography>
      )}
    </Box>
  );
}

// Each section and check by its plain name; an unknown one is made readable.
export const CHECK_NAMES: Record<string, string> = {
  outbound: 'Sending', inbound: 'Receiving', system: 'This server', storage: 'Storage', security: 'Security',
  plugins: 'Provider plugins', backend: 'Provider', backend_config: 'Provider settings complete',
  configuration_ready: 'Settings ready', ami_connected: 'Fax engine connected', ami_connection: 'Fax engine connected',
  requires_ami: 'Needs the fax engine', sending_disabled: 'Sending turned off', enabled: 'Turned on',
  ami_password_not_default: 'Fax engine password changed from the default',
  ami_password_secure: 'Fax engine password changed from the default',
  asterisk_secret_set: 'Fax engine secret for received faxes set', inbound_verification: 'Received faxes checked',
  retention_days: 'Days received faxes are kept', db: 'Database', database_connected: 'Database reachable',
  ghostscript: 'Document converter (Ghostscript)', fax_data_dir: 'Fax data folder',
  fax_data_writable: 'Fax data folder can be written', temp_dir_writable: 'Temporary folder can be written',
  type: 'Kind', needs_storage: 'Storage needed', required_for_active_inbound: 'Needed for receiving',
  bucket: 'Bucket', bucket_set: 'Bucket set', accessible: 'Bucket reachable', error: 'Problem',
  enforce_https: 'HTTPS required for document links', audit_logging: 'Events recorded', rate_limiting: 'Request limits on',
  pdf_token_ttl: 'Document links for fax services last (minutes)', max_attempts: 'Most tries per fax',
  v3_enabled: 'Provider plugins on', plugin_install_enabled: 'Remote plugin installation allowed',
  active_outbound: 'Plugin that sends', installed: 'Installed plugins', manifests: 'Plugin files',
  traits_schema: 'Plugin descriptions', allowed_keys: 'Allowed keys', allowed_domains: 'Allowed domains',
  inspection_error: 'Problem reading plugins', storage_error: 'Storage problem', warnings: 'Warnings',
};

export function checkName(name: string): string {
  return CHECK_NAMES[name] ?? name.replace(/_/g, ' ').replace(/^\w/, (letter) => letter.toUpperCase());
}

function Diagnostics({ client, onNavigate, docsBase }: DiagnosticsProps) {
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [helpSection, setHelpSection] = useState('');
  const [helpKey, setHelpKey] = useState('');
  const [expandedSections, setExpandedSections] = useState<string[]>([]);
  const [restartState, setRestartState] = useState<'idle' | 'pending' | 'success' | 'error'>('idle');
  const [restartMessage, setRestartMessage] = useState('');
  const [exportNotice, setExportNotice] = useState<ActionNotice | null>(null);
  const [copying, setCopying] = useState(false);

  const theme = useTheme();
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

  const runDiagnostics = async () => {
    if (loading) return;
    try {
      setError(null);
      setExportNotice(null);
      setLoading(true);
      const data = await client.runDiagnostics();
      setDiagnostics(data);
      setExpandedSections(Object.keys(data.checks));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to run diagnostics');
    } finally {
      setLoading(false);
    }
  };

  const requestRestart = async () => {
    if (restartState === 'pending') return;
    setRestartState('pending');
    setRestartMessage('Requesting restart…');
    try {
      const result = await client.restart();
      if (result?.ok !== true) throw new Error('The restart request was not accepted.');
      setRestartMessage('Restart requested. Run diagnostics again once Faxbot is back.');
      setRestartState('success');
    } catch (err) {
      setRestartMessage(err instanceof Error ? err.message : "Couldn't restart Faxbot.");
      setRestartState('error');
    }
  };

  const copyDiagnostics = async () => {
    if (!diagnostics || copying) return;
    setCopying(true);
    setExportNotice(null);
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard access is unavailable in this browser. Use Download instead.');
      await navigator.clipboard.writeText(JSON.stringify(diagnosticsForExport(diagnostics), null, 2));
      setExportNotice({ severity: 'success', text: 'Diagnostics copied.' });
    } catch (err) {
      setExportNotice({ severity: 'error', text: err instanceof Error ? err.message : 'Diagnostics could not be copied.' });
    } finally {
      setCopying(false);
    }
  };

  const downloadDiagnostics = () => {
    if (!diagnostics) return;
    let url: string | undefined;
    let anchor: HTMLAnchorElement | undefined;
    setExportNotice(null);
    try {
      const blob = new Blob([JSON.stringify(diagnosticsForExport(diagnostics), null, 2)], { type: 'application/json;charset=utf-8' });
      url = URL.createObjectURL(blob);
      anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'diagnostics.json';
      document.body.appendChild(anchor);
      anchor.click();
      setExportNotice({ severity: 'info', text: 'Diagnostics downloaded.' });
    } catch (err) {
      setExportNotice({ severity: 'error', text: err instanceof Error ? err.message : 'Diagnostics could not be downloaded.' });
    } finally {
      anchor?.remove();
      if (url) {
        const downloadUrl = url;
        // Let the browser consume the click before releasing the temporary URL.
        setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
      }
    }
  };

  const handleSectionToggle = (section: string) => {
    setExpandedSections(prev => prev.includes(section) ? prev.filter(s => s !== section) : [...prev, section]);
  };

  const displayName = checkName;

  const getHelpDocs = (section: string) => {
    const docs = [{ text: 'Settings guide', href: docsLink('storage', docsBase) }];
    if (section === 'system') docs.push({ text: 'Deployment guide', href: docsLink('deployment', docsBase) });
    if (section === 'security') docs.push({ text: 'Security guide', href: docsLink('security', docsBase) });
    if (section === 'inbound') docs.push({ text: 'Receiving guide', href: docsLink('inbound', docsBase) });
    const provider = section === 'outbound' ? diagnostics?.outbound_backend : section === 'inbound' ? diagnostics?.inbound_backend : section;
    switch (provider) {
      case 'phaxio': docs.push({ text: 'Phaxio setup guide', href: docsLink('phaxio', docsBase) }); break;
      case 'sinch': docs.push({ text: 'Sinch setup guide', href: docsLink('sinch', docsBase) }); break;
      case 'documo': docs.push({ text: 'Documo setup guide', href: docsLink('documo', docsBase) }); break;
      case 'humblefax': docs.push({ text: 'HumbleFax setup guide', href: docsLink('humblefax', docsBase) }); break;
      case 'efax': docs.push({ text: 'eFax setup guide', href: docsLink('efax', docsBase) }); break;
      case 'signalwire': docs.push({ text: 'SignalWire setup guide', href: docsLink('signalwire', docsBase) }); break;
      case 'freeswitch': docs.push({ text: 'FreeSWITCH setup guide', href: docsLink('freeswitch', docsBase) }); break;
      case 'sip': docs.push({ text: 'Carrier trunk setup guide', href: docsLink('sip', docsBase) }); break;
    }
    return docs;
  };

  const renderCheckSection = (section: string, checks: Record<string, DiagnosticsValue>) => {
    const entries = Object.entries(checks);
    const outcomes = entries.map(([key]) => explicitOutcome(diagnostics?.check_outcomes?.[section]?.[key]));
    const failCount = outcomes.filter(outcome => outcome === 'fail').length;
    const warningCount = outcomes.filter(outcome => outcome === 'warning').length;
    const passCount = outcomes.filter(outcome => outcome === 'pass').length;
    const infoCount = outcomes.filter(outcome => outcome === 'info').length;
    const notApplicableCount = outcomes.filter(outcome => outcome === 'not_applicable').length;
    const sectionIcon = failCount > 0 ? <ErrorIcon color="error" /> : warningCount > 0 ? <WarningIcon color="warning" /> : passCount > 0 && infoCount === 0 && notApplicableCount === 0 ? <CheckCircleIcon color="success" /> : <InfoIcon color="action" />;
    const counts = [
      passCount > 0 ? `${passCount} Pass` : '',
      failCount > 0 ? `${failCount} Fail` : '',
      warningCount > 0 ? `${warningCount} Warning` : '',
      infoCount > 0 ? `${infoCount} Info` : '',
      notApplicableCount > 0 ? `${notApplicableCount} Not applicable` : '',
    ].filter(Boolean).join(' · ');

    return (
      <Accordion
        key={section}
        expanded={expandedSections.includes(section)}
        onChange={() => handleSectionToggle(section)}
        elevation={0}
        sx={{ mb: 2, '&:before': { display: 'none' }, border: '1px solid', borderColor: 'divider' }}
      >
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', minWidth: 0, width: '100%' }}>
            {sectionIcon}
            <Typography variant="h6" fontWeight={600}>{displayName(section)}</Typography>
            <Typography variant="caption" color="text.secondary" sx={{ ml: { sm: 'auto' }, overflowWrap: 'anywhere' }}>
              {counts || 'No checks'}
            </Typography>
          </Box>
        </AccordionSummary>
        <AccordionDetails>
          <Stack spacing={2}>
            {entries.map(([key, value]) => {
              const outcome = explicitOutcome(diagnostics?.check_outcomes?.[section]?.[key]);
              return (
                <Paper
                  key={key}
                  elevation={0}
                  sx={{ p: 2, minWidth: 0, border: '1px solid', borderColor: outcome === 'fail' ? 'error.main' : outcome === 'warning' ? 'warning.main' : 'divider', borderRadius: 2 }}
                >
                  <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, minWidth: 0 }}>
                    <Box sx={{ flex: 1, minWidth: 0 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                        <Typography variant="subtitle2" fontWeight={600} sx={{ overflowWrap: 'anywhere' }}>{displayName(key)}</Typography>
                        <OutcomeChip outcome={outcome} />
                      </Box>
                      <CheckValue value={value} />
                    </Box>
                    <Tooltip title="Help">
                      <IconButton size="small" aria-label={`Help for ${displayName(section)} ${displayName(key)}`} onClick={() => {
                        setHelpSection(section);
                        setHelpKey(key);
                        setHelpOpen(true);
                      }}>
                        <HelpIcon />
                      </IconButton>
                    </Tooltip>
                  </Box>
                </Paper>
              );
            })}
          </Stack>
        </AccordionDetails>
      </Accordion>
    );
  };

  const issues = diagnostics ? [
    ...diagnostics.summary.critical_issues.map(text => ({ severity: 'error' as const, text })),
    ...diagnostics.summary.warnings.map(text => ({ severity: 'warning' as const, text: PLAIN_NOTES[text] ?? text })),
  ] : [];

  return (
    <>
      <Box>
        <Box display="flex" justifyContent="space-between" alignItems={{ xs: 'flex-start', sm: 'center' }} flexDirection={{ xs: 'column', sm: 'row' }} gap={2} mb={3}>
          <Typography variant="h4" component="h1">System Diagnostics</Typography>
          <Box display="flex" gap={1} flexWrap="wrap">
            <Button variant="contained" startIcon={loading ? <CircularProgress size={20} color="inherit" /> : <DiagnosticIcon />} onClick={runDiagnostics} disabled={loading} size={isSmallMobile ? 'medium' : 'large'} sx={{ borderRadius: 2 }}>
              {loading ? 'Running…' : 'Run Diagnostics'}
            </Button>
            <Button variant="outlined" startIcon={restartState === 'pending' ? <CircularProgress size={20} color="inherit" /> : <RestartIcon />} onClick={requestRestart} disabled={restartState === 'pending'} size={isSmallMobile ? 'medium' : 'large'} sx={{ borderRadius: 2 }}>
              {restartState === 'pending' ? 'Requesting…' : 'Restart Faxbot'}
            </Button>
          </Box>
        </Box>

        {error && <Alert severity="error" sx={{ mb: 3, borderRadius: 2 }} onClose={() => setError(null)}>{error}</Alert>}
        {restartState !== 'idle' && (
          <Alert severity={restartState === 'error' ? 'error' : restartState === 'success' ? 'success' : 'info'} sx={{ mb: 3, borderRadius: 2 }} onClose={restartState === 'pending' ? undefined : () => setRestartState('idle')}>
            {restartMessage}
          </Alert>
        )}

        <DatabaseStatus client={client} />

        {!diagnostics && !loading && (
          <Fade in>
            <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 2 }}>
              <HealthIcon sx={{ fontSize: 48, color: 'text.secondary', mb: 2 }} />
              <Typography variant="h6" gutterBottom>Run System Diagnostics</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
                Check this installation's settings and the services it needs. No fax is sent.
              </Typography>
              <Button variant="contained" startIcon={<DiagnosticIcon />} onClick={runDiagnostics} sx={{ borderRadius: 2 }}>Start Diagnostics</Button>
            </Paper>
          </Fade>
        )}

        {loading && (
          <Paper sx={{ p: 4, borderRadius: 2, mb: 3 }}>
            <Box sx={{ textAlign: 'center' }}>
              <CircularProgress sx={{ mb: 2 }} />
              <Typography variant="body1">Running diagnostics…</Typography>
              <Typography variant="caption" color="text.secondary">Checking settings and services</Typography>
            </Box>
            <LinearProgress sx={{ mt: 3 }} />
          </Paper>
        )}

        {diagnostics && (
          <Fade in>
            <Box>
              <ResponsiveFormSection title="Summary" subtitle={diagnostics.summary.healthy ? 'Ready' : 'Issues detected'} icon={<HealthIcon />}>
                <Stack spacing={3}>
                  <Box>
                    <Chip icon={diagnostics.summary.healthy ? <CheckCircleIcon /> : <ErrorIcon />} label={diagnostics.summary.healthy ? 'Ready' : 'Issues detected'} color={diagnostics.summary.healthy ? 'success' : 'error'} sx={{ borderRadius: 1 }} />
                  </Box>
                  <Box sx={{ overflowWrap: 'anywhere' }}>
                    <Typography variant="body2">Sending: <strong>{diagnostics.outbound_backend ? providerLabel(diagnostics.outbound_backend) : 'No provider'}</strong></Typography>
                    <Typography variant="body2">Receiving: <strong>{diagnostics.inbound_backend ? providerLabel(diagnostics.inbound_backend) : 'No provider'}</strong></Typography>
                    <Typography variant="body2">Main provider: <strong>{diagnostics.default_backend ? providerLabel(diagnostics.default_backend) : 'No provider'}</strong></Typography>
                    <Typography variant="body2">Saved changes waiting for a restart: <strong>{diagnostics.configuration.pending_restart ? 'Yes' : 'No'}</strong></Typography>
                    <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>Checked at {formatServerTime(diagnostics.timestamp)}</Typography>
                  </Box>
                  {issues.length > 0 && (
                    <Box>
                      <Typography variant="subtitle2" fontWeight={600}>Issues</Typography>
                      <List dense>
                        {issues.map((issue, index) => (
                          <ListItem key={`${issue.severity}-${index}`} sx={{ px: 0, alignItems: 'flex-start' }}>
                            <ListItemIcon sx={{ minWidth: 36 }}>{issue.severity === 'error' ? <ErrorIcon color="error" /> : <WarningIcon color="warning" />}</ListItemIcon>
                            <ListItemText primary={issue.text} primaryTypographyProps={{ variant: 'body2', color: issue.severity === 'error' ? 'error' : 'text.primary', sx: { overflowWrap: 'anywhere' } }} />
                          </ListItem>
                        ))}
                      </List>
                    </Box>
                  )}
                  <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                    {onNavigate && <Button variant="outlined" onClick={() => onNavigate('settings')} size="small">Open Settings</Button>}
                    <Button variant="outlined" startIcon={<ContentCopyIcon />} onClick={copyDiagnostics} disabled={copying} size="small">{copying ? 'Copying…' : 'Copy results'}</Button>
                    <Button variant="outlined" startIcon={<DownloadIcon />} onClick={downloadDiagnostics} size="small">Download results</Button>
                  </Box>
                  {exportNotice && <Alert severity={exportNotice.severity} onClose={() => setExportNotice(null)}>{exportNotice.text}</Alert>}
                </Stack>
              </ResponsiveFormSection>

              <ResponsiveFormSection title="Test a fax through Send" subtitle="Choose your own document and destination" icon={<SendIcon />}>
                <Stack spacing={2}>
                  <Typography variant="body2" color="text.secondary">
                    Send a test fax to a number you control, then follow it in Sent.
                  </Typography>
                  <Box>
                    <Button variant="outlined" startIcon={<SendIcon />} onClick={() => onNavigate?.('send')} disabled={!onNavigate} sx={{ borderRadius: 2 }}>Open Send</Button>
                  </Box>
                </Stack>
              </ResponsiveFormSection>

              <Box sx={{ mt: 3 }}>
                <Typography variant="h6" fontWeight={600} sx={{ mb: 1 }}>System Checks</Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                  Info items don't affect the overall status.
                </Typography>
                {Object.entries(diagnostics.checks).map(([section, checks]) => renderCheckSection(section, checks))}
              </Box>
            </Box>
          </Fade>
        )}
      </Box>

      <Dialog open={helpOpen} onClose={() => setHelpOpen(false)} maxWidth="sm" fullWidth fullScreen={isSmallMobile}>
        <DialogTitle>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}><HelpIcon />Help: {displayName(helpSection)} — {displayName(helpKey)}</Box>
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2}>
            <Typography variant="body2">
              Provider and receiving options are in Settings; the guides below cover setup in detail.
            </Typography>
            {getHelpDocs(helpSection).map(doc => <Link key={doc.href} href={doc.href} target="_blank" rel="noreferrer">{doc.text}</Link>)}
          </Stack>
        </DialogContent>
        <DialogActions>
          {onNavigate && <Button onClick={() => { setHelpOpen(false); onNavigate('settings'); }}>Open Settings</Button>}
          <Button onClick={() => setHelpOpen(false)} sx={{ borderRadius: 2 }}>Close</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}

export default Diagnostics;
