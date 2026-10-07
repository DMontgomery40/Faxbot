import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Link, Stack, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../api/client';
import type { TelnyxNamesReport } from '../api/networkTypes';
import { readOnText } from './delivery/ReceivingRecommendations';

interface TelnyxNamesProps {
  client: AdminAPIClient;
  // Anything that changes when the trunk was applied or checked; the section reads the check again.
  refresh?: unknown;
  canChange?: boolean;
}

// Whether Telnyx looks up callers' names on each trunk number, at its published price, with a button that turns it
// off for one number. Faxbot never shows callers' names, so the lookup buys nothing here.
function TelnyxNames({ client, refresh, canChange = true }: TelnyxNamesProps) {
  const [report, setReport] = useState<TelnyxNamesReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ severity: 'success' | 'warning' | 'error'; text: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const result = await client.getTelnyxNames();
      if (alive.current) setReport(result);
    } catch (error) {
      if (alive.current && !isForbidden(error)) setReport(null);
    }
  }, [client]);
  useEffect(() => { void load(); }, [load, refresh]);

  const turnOff = async (number: string) => {
    setBusy(number);
    setNotice(null);
    try {
      const result = await client.turnOffTelnyxNameLookup(number);
      if (!alive.current) return;
      setReport(result);
      setNotice({ severity: result.outcome === 'off' ? 'success' : 'warning', text: result.message ?? '' });
    } catch (error) {
      const text = error instanceof AdminAPIError && error.status === 403 ? 'You do not have permission to do this.'
        : error instanceof AdminAPIError && error.detail ? error.detail : 'Telnyx could not be changed. Try again.';
      if (alive.current) setNotice({ severity: 'error', text });
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  if (!report?.applies) return null;
  const numbers = report.numbers ?? [];
  const on = numbers.filter((entry) => entry.lookup === true);
  const unknown = numbers.filter((entry) => entry.lookup === null);
  return (
    <Box data-testid="telnyx-names">
      <Typography variant="subtitle2">Caller-name lookup at Telnyx</Typography>
      <Alert severity={on.length ? 'info' : 'success'} sx={{ mt: 1 }}>
        {on.length === 0 && <Typography variant="body2">{report.text}</Typography>}
        {[...on, ...(on.length ? [] : unknown.filter((entry) => entry.text !== report.text))].map((entry) => (
          <Stack key={entry.number} direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}
            sx={{ mb: 0.5 }}>
            <Typography variant="body2">{entry.text}</Typography>
            {entry.can_turn_off && canChange && (
              <Button size="small" variant="outlined" disabled={busy !== null} onClick={() => { void turnOff(entry.number); }}
                startIcon={busy === entry.number ? <CircularProgress size={14} color="inherit" /> : undefined}>
                {`Turn off name lookup for ${entry.display}`}
              </Button>
            )}
          </Stack>
        ))}
        <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 0.5 }}>
          {`Telnyx's price: ${report.price.text}, read ${readOnText(report.price.read_on)} from `}
          <Link href={report.price.source_url} target="_blank" rel="noopener noreferrer">Telnyx's number lookup guide</Link>.
        </Typography>
      </Alert>
      {notice && <Alert severity={notice.severity} sx={{ mt: 1 }}>{notice.text}</Alert>}
    </Box>
  );
}

export default TelnyxNames;
