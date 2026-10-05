import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Stack, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../api/client';
import type { TelnyxT38Report } from '../api/networkTypes';

interface TelnyxT38Props {
  client: AdminAPIClient;
  // Anything that changes when the trunk was applied or checked; the section reads the check again.
  refresh?: unknown;
}

// Whether Telnyx accepts fax over IP (T.38) on each trunk number, with a button that turns it on for one number.
function TelnyxT38({ client, refresh }: TelnyxT38Props) {
  const [report, setReport] = useState<TelnyxT38Report | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ severity: 'success' | 'warning' | 'error'; text: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const result = await client.getTelnyxT38();
      if (alive.current) setReport(result);
    } catch (error) {
      if (alive.current && !isForbidden(error)) setReport(null);
    }
  }, [client]);
  useEffect(() => { void load(); }, [load, refresh]);

  const turnOn = async (number: string) => {
    setBusy(number);
    setNotice(null);
    try {
      const result = await client.turnOnTelnyxT38(number);
      if (!alive.current) return;
      setReport(result);
      setNotice({ severity: result.outcome === 'on' ? 'success' : 'warning', text: result.message ?? '' });
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
  const lines = numbers.filter((entry) => entry.state !== 'on');
  return (
    <Box data-testid="telnyx-t38">
      <Typography variant="subtitle2">Fax over IP (T.38) at Telnyx</Typography>
      <Alert severity={report.ready ? 'success' : 'warning'} sx={{ mt: 1 }}>
        {lines.length === 0 && <Typography variant="body2">{report.text}</Typography>}
        {lines.map((entry) => (
          <Stack key={entry.number} direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}
            sx={{ mb: 0.5 }}>
            <Typography variant="body2">{entry.text}</Typography>
            {entry.fixable && (
              <Button size="small" variant="outlined" disabled={busy !== null} onClick={() => { void turnOn(entry.number); }}
                startIcon={busy === entry.number ? <CircularProgress size={14} color="inherit" /> : undefined}>
                {`Turn on T.38 for ${entry.display}`}
              </Button>
            )}
          </Stack>
        ))}
        {(report.connection_texts ?? []).map((text) => <Typography key={text} variant="body2">{text}</Typography>)}
      </Alert>
      {notice && <Alert severity={notice.severity} sx={{ mt: 1 }}>{notice.text}</Alert>}
    </Box>
  );
}

export default TelnyxT38;
