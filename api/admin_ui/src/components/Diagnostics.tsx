import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Paper,
  Stack,
  Typography,
} from '@mui/material';
import {
  CheckCircle as CheckCircleIcon,
  ContentCopy as ContentCopyIcon,
  Download as DownloadIcon,
  Error as ErrorIcon,
  RemoveCircleOutline as OffIcon,
  RestartAlt as RestartIcon,
  Refresh as RefreshIcon,
  Warning as WarningIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { DiagnosticsFinding, DiagnosticsReport, DiagnosticsStatus } from '../api/types';
import type { AdminDestination } from '../navigation';
import DatabaseStatus from './DatabaseStatus';
import ReceivingReadiness from './ReceivingReadiness';
import RouteFamilies from './RouteFamilies';
import PowerCheck from './PowerCheck';
import TestLines from './TestLines';

interface DiagnosticsProps {
  client: AdminAPIClient;
  onNavigate?: (destination: AdminDestination) => void;
  docsBase?: string;
}

type Notice = { severity: 'success' | 'error' | 'info'; text: string };

const STATUS_WORDS: Record<DiagnosticsStatus, string> = {
  ok: 'Working', attention: 'Needs attention', problem: 'Not working', off: 'Not in use',
};

function StatusIcon({ status }: { status: DiagnosticsStatus }) {
  const label = STATUS_WORDS[status];
  if (status === 'ok') return <CheckCircleIcon color="success" titleAccess={label} />;
  if (status === 'attention') return <WarningIcon color="warning" titleAccess={label} />;
  if (status === 'problem') return <ErrorIcon color="error" titleAccess={label} />;
  return <OffIcon color="disabled" titleAccess={label} />;
}

// What Copy hands out: the same sentences a person reads on the page.
export function reportAsText(report: DiagnosticsReport): string {
  const lines = [`Faxbot diagnostics, ${report.checked_at_text}`, report.summary ?? '', ''];
  for (const section of report.sections) {
    lines.push(section.title);
    for (const item of section.checks) lines.push(`- ${item.title}: ${STATUS_WORDS[item.status]}. ${item.sentence}`);
    lines.push('');
  }
  return lines.join('\n').trim() + '\n';
}

function Finding({ item, onNavigate }: { item: DiagnosticsFinding; onNavigate?: (destination: AdminDestination) => void }) {
  return (
    <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-start', py: 1.25, borderTop: '1px solid', borderColor: 'divider',
      '&:first-of-type': { borderTop: 'none' } }}>
      <Box sx={{ pt: 0.25 }}><StatusIcon status={item.status} /></Box>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography variant="subtitle2" fontWeight={600}>{item.title}</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ overflowWrap: 'anywhere' }}>{item.sentence}</Typography>
      </Box>
      {item.fix && item.fix.page && onNavigate && (
        <Button size="small" variant="outlined" sx={{ flexShrink: 0 }}
          onClick={() => onNavigate(item.fix!.page as AdminDestination)}>
          {item.fix.label}
        </Button>
      )}
    </Box>
  );
}

function Diagnostics({ client, onNavigate }: DiagnosticsProps) {
  const [report, setReport] = useState<DiagnosticsReport | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [restartState, setRestartState] = useState<'idle' | 'pending' | 'success' | 'error'>('idle');
  const [restartMessage, setRestartMessage] = useState('');
  const [reloading, setReloading] = useState(false);
  const started = useRef(false);

  const checkNow = useCallback(async () => {
    setChecking(true);
    setError(null);
    setNotice(null);
    try {
      setReport(await client.checkDiagnosticsNow());
    } catch {
      setError('Faxbot could not finish the checks. Try again in a moment.');
    } finally {
      setChecking(false);
    }
  }, [client]);

  // Show the last run at once; the first visit after Faxbot starts checks by itself.
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    client.getDiagnosticsReport().then((last) => {
      if (last.checked_at) setReport(last);
      else void checkNow();
    }).catch(() => { void checkNow(); });
  }, [client, checkNow]);

  const requestRestart = async () => {
    if (restartState === 'pending') return;
    setRestartState('pending');
    setRestartMessage('Asking Faxbot to restart…');
    try {
      const result = await client.restart();
      if (result?.ok !== true) throw new Error('Faxbot did not accept the restart request.');
      setRestartMessage('Faxbot is restarting. Check again once it is back.');
      setRestartState('success');
    } catch (err) {
      setRestartMessage(err instanceof Error ? err.message : "Faxbot couldn't restart.");
      setRestartState('error');
    }
  };

  const reloadSaved = async () => {
    if (reloading) return;
    setReloading(true);
    setNotice(null);
    try {
      await client.reloadSettings();
      client.announceSettingsChanged();
      setNotice({ severity: 'success', text: 'Faxbot read its saved settings again. Changes waiting for a restart still wait.' });
    } catch {
      setNotice({ severity: 'error', text: 'The saved settings could not be read again. Try again in a moment.' });
    } finally {
      setReloading(false);
    }
  };

  const copy = async () => {
    if (!report) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('This browser does not allow copying. Use Download instead.');
      await navigator.clipboard.writeText(reportAsText(report));
      setNotice({ severity: 'success', text: 'Results copied.' });
    } catch (err) {
      setNotice({ severity: 'error', text: err instanceof Error ? err.message : 'The results could not be copied.' });
    }
  };

  const download = () => {
    if (!report) return;
    const url = URL.createObjectURL(new Blob([reportAsText(report)], { type: 'text/plain;charset=utf-8' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'faxbot-diagnostics.txt';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setNotice({ severity: 'info', text: 'Results downloaded.' });
  };

  const verdictSeverity = report?.status === 'problem' ? 'error' : report?.status === 'attention' ? 'warning' : 'success';

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems={{ xs: 'flex-start', sm: 'center' }}
        flexDirection={{ xs: 'column', sm: 'row' }} gap={2} mb={1}>
        <Typography variant="h4" component="h1">Diagnostics</Typography>
        <Box display="flex" gap={1} flexWrap="wrap">
          <Button variant="contained" onClick={() => void checkNow()} disabled={checking}
            startIcon={checking ? <CircularProgress size={18} color="inherit" /> : <RefreshIcon />}>
            {checking ? 'Checking…' : 'Check now'}
          </Button>
          <Button variant="outlined" onClick={requestRestart} disabled={restartState === 'pending'} startIcon={<RestartIcon />}>
            Restart Faxbot
          </Button>
        </Box>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Checks sending, receiving, the fax engine, this server and security. No fax is sent and nothing is changed.
        {report?.checked_at_text ? ` Last checked ${report.checked_at_text}.` : ''}
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>{error}</Alert>}
      {restartState !== 'idle' && (
        <Alert severity={restartState === 'error' ? 'error' : restartState === 'success' ? 'success' : 'info'} sx={{ mb: 2 }}
          onClose={restartState === 'pending' ? undefined : () => setRestartState('idle')}>{restartMessage}</Alert>
      )}
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }} onClose={() => setNotice(null)}>{notice.text}</Alert>}

      {!report && checking && (
        <Paper sx={{ p: 4, textAlign: 'center', mb: 3 }}>
          <CircularProgress sx={{ mb: 2 }} />
          <Typography>Checking sending, receiving and this server…</Typography>
        </Paper>
      )}

      {report && report.summary && (
        <>
          <Alert severity={verdictSeverity} sx={{ mb: 3 }}
            action={(
              <Stack direction="row" spacing={1}>
                <Button size="small" color="inherit" startIcon={<ContentCopyIcon />} onClick={() => void copy()}>Copy results</Button>
                <Button size="small" color="inherit" startIcon={<DownloadIcon />} onClick={download}>Download</Button>
              </Stack>
            )}>
            {report.summary}
          </Alert>
          <Stack spacing={2} sx={{ mb: 4 }}>
            {report.sections.map((section) => (
              <Paper key={section.id} variant="outlined" sx={{ px: 2, py: 1.5 }}>
                <Typography variant="h6" component="h2" sx={{ mb: 0.5 }}>{section.title}</Typography>
                {section.checks.map((item) => <Finding key={item.id} item={item} onNavigate={onNavigate} />)}
              </Paper>
            ))}
          </Stack>
        </>
      )}

      <ReceivingReadiness client={client} />
      <RouteFamilies client={client} />
      <PowerCheck client={client} />
      <TestLines client={client} />

      <DatabaseStatus client={client} />

      <Box sx={{ mt: 2 }}>
        <Button variant="text" size="small" onClick={reloadSaved} disabled={reloading}>
          {reloading ? 'Reading…' : 'Read saved settings again'}
        </Button>
      </Box>
    </Box>
  );
}

export default Diagnostics;
