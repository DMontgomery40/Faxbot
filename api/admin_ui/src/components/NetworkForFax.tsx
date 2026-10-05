import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Stack, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../api/client';
import type { SipNetworkReport } from '../api/networkTypes';

interface NetworkForFaxProps {
  client: AdminAPIClient;
  // Called after Check again turned T.38 on or off, so the trunk form shows the new setting.
  onChanged?: () => void | Promise<void>;
  // Anything that changes when the trunk was applied or checked; the section reads the check again.
  refresh?: unknown;
}

// What Faxbot did to T.38 because of the network, in one sentence with the day in the reader's own words.
export function actionSentence(action: SipNetworkReport['action'], at?: string | null): string | null {
  const date = at ? new Date(at) : null;
  const who = date && !Number.isNaN(date.getTime())
    ? `On ${date.toLocaleDateString(undefined, { day: 'numeric', month: 'long' })}, Faxbot` : 'Faxbot';
  if (action === 'turned_off') return `${who} switched new calls to audio fax.`;
  if (action === 'turned_on') return `${who} switched new calls back to T.38 fax because your network allows it now.`;
  return null;
}

const SEVERITY = { open: 'success', blocked: 'warning', unknown: 'info' } as const;

// The trunk page's "Network for fax over IP": the verdict, what Faxbot did, the fix, and Check again.
function NetworkForFax({ client, onChanged, refresh }: NetworkForFaxProps) {
  const [report, setReport] = useState<SipNetworkReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'info' | 'error'; text: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const result = await client.getSipNetwork();
      if (alive.current) setReport(result);
    } catch (error) {
      if (alive.current && !isForbidden(error)) {
        setNotice({ severity: 'error', text: 'The network check could not be loaded. Try again.' });
      }
    }
  }, [client]);
  useEffect(() => { void load(); }, [load, refresh]);

  const checkAgain = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await client.checkSipNetwork();
      if (!alive.current) return;
      setReport(result);
      if (result.engine_message) setNotice({ severity: 'info', text: result.engine_message });
      if (result.switched) await onChanged?.();
    } catch (error) {
      const text = error instanceof AdminAPIError && error.status === 403 ? 'You do not have permission to do this.'
        : error instanceof AdminAPIError && error.status === 400 && error.detail ? error.detail
          : 'The network could not be checked. Try again.';
      if (alive.current) setNotice({ severity: 'error', text });
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText((report?.fix_steps ?? []).join('\n'));
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  if (!report?.applies) return notice ? <Alert severity={notice.severity}>{notice.text}</Alert> : null;
  const done = actionSentence(report.action, report.action_at);
  const steps = report.fix_steps ?? [];
  const checkedAt = report.checked_at ? new Date(report.checked_at) : null;
  return (
    <Box data-testid="sip-network">
      <Typography variant="subtitle2">Network for fax over IP</Typography>
      <Alert severity={SEVERITY[report.t38 ?? 'unknown'] ?? 'info'} sx={{ mt: 1 }}>
        <Typography variant="body2" fontWeight={600}>{report.text}</Typography>
        {report.platform_text && <Typography variant="body2">{report.platform_text}</Typography>}
        {done && <Typography variant="body2">{done}</Typography>}
        {report.fix_text && <Typography variant="body2" sx={{ mt: 1 }}>{report.fix_text}</Typography>}
        {steps.length > 0 && (
          <>
            <Box component="code" data-testid="sip-network-steps" sx={{ display: 'block', mt: 0.5, p: 1, borderRadius: 1,
              bgcolor: 'action.hover', fontFamily: 'monospace', fontSize: '0.85rem', overflowX: 'auto',
              whiteSpace: 'pre' }}>
              {steps.join('\n')}
            </Box>
            <Button size="small" onClick={copy} sx={{ mt: 0.5 }}>{copied ? 'Copied' : 'Copy'}</Button>
          </>
        )}
        {report.fix_note && <Typography variant="body2" color="text.secondary">{report.fix_note}</Typography>}
        {report.audio_text && <Typography variant="body2" sx={{ mt: 1 }}>{report.audio_text}</Typography>}
      </Alert>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1 }}>
        <Button size="small" variant="outlined" onClick={checkAgain} disabled={busy}
          startIcon={busy ? <CircularProgress size={14} color="inherit" /> : undefined}>Check again</Button>
        {checkedAt && !Number.isNaN(checkedAt.getTime()) && (
          <Typography variant="caption" color="text.secondary">{`Checked ${checkedAt.toLocaleString()}`}</Typography>
        )}
      </Stack>
      {notice && <Alert severity={notice.severity} sx={{ mt: 1 }}>{notice.text}</Alert>}
    </Box>
  );
}

export default NetworkForFax;
