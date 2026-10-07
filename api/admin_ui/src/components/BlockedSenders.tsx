import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  TextField, Typography,
} from '@mui/material';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../api/client';
import type { BlockedSendersView } from '../api/numbersTypes';
import { formatServerTime } from '../api/time';

function message(error: unknown, fallback: string) {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

interface BlockedSendersProps {
  client: AdminAPIClient;
  canWrite: boolean;
}

// Numbers → Blocked senders: junk senders turned away before the call is answered, and every call turned away.
function BlockedSenders({ client, canWrite }: BlockedSendersProps) {
  const [view, setView] = useState<BlockedSendersView | null>(null);
  const [number, setNumber] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const result = await client.getBlockedSenders();
      if (alive.current) setView(result);
    } catch (error) {
      if (alive.current && !isForbidden(error)) setNotice({ severity: 'error', text: message(error, 'Faxbot could not read the blocked senders. Try again.') });
    }
  }, [client]);
  useEffect(() => { void load(); }, [load]);

  const run = async (action: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setNotice(null);
    try {
      await action();
      if (!alive.current) return;
      setNotice({ severity: 'success', text: done });
      setNumber('');
      setReason('');
      await load();
    } catch (error) {
      if (alive.current) setNotice({ severity: 'error', text: message(error, 'Nothing was changed. Try again.') });
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const entries = view?.entries ?? [];
  const current = entries.filter((entry) => entry.active);
  const past = entries.filter((entry) => !entry.active);
  return (
    <Box data-testid="blocked-senders">
      <Typography variant="h5" gutterBottom>Blocked senders</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Calls from these numbers are turned away before Faxbot answers, so the call costs nothing and no junk fax arrives.
        Callers who withhold their number are never blocked.
      </Typography>
      {view && <Alert severity={view.synced || current.length === 0 ? 'info' : 'warning'} sx={{ mb: 2 }}>{view.sentence}</Alert>}
      {notice && <Alert severity={notice.severity} sx={{ mb: 2 }}>{notice.text}</Alert>}

      {canWrite && (
        <Card sx={{ mb: 3 }}>
          <CardContent>
            <Typography variant="subtitle1" gutterBottom>Block a number</Typography>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'flex-start' }}>
              <TextField size="small" label="Number" value={number} onChange={(event) => setNumber(event.target.value)}
                placeholder="+13035550142" />
              <TextField size="small" label="Why it is junk" value={reason} sx={{ flex: 1 }} inputProps={{ maxLength: 200 }}
                onChange={(event) => setReason(event.target.value)} helperText="Blocked for 90 days." />
              <Button variant="contained" disabled={busy || !number.trim() || !reason.trim()}
                onClick={() => { void run(() => client.blockSender({ number: number.trim(), reason: reason.trim() }), 'Blocked for 90 days.'); }}>
                Block
              </Button>
            </Stack>
          </CardContent>
        </Card>
      )}

      <TableContainer component={Card} sx={{ mb: 3 }}>
        <Table size="small" aria-label="Blocked numbers">
          <TableHead>
            <TableRow>
              <TableCell>Number</TableCell>
              <TableCell>Why</TableCell>
              <TableCell>Blocked by</TableCell>
              <TableCell>Until</TableCell>
              <TableCell>Calls turned away</TableCell>
              <TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {current.length === 0 && (
              <TableRow><TableCell colSpan={6}>No sender is blocked.</TableCell></TableRow>
            )}
            {current.map((entry) => (
              <TableRow key={entry.id}>
                <TableCell>{entry.number}</TableCell>
                <TableCell>{entry.reason}</TableCell>
                <TableCell>{entry.added_by ?? 'An integration key'}, {formatServerTime(entry.added_at)}</TableCell>
                <TableCell>{formatServerTime(entry.expires_at)}</TableCell>
                <TableCell>{entry.rejected_calls}</TableCell>
                <TableCell align="right">
                  {canWrite && (
                    <Button size="small" disabled={busy} aria-label={`Unblock ${entry.number}`}
                      onClick={() => { void run(() => client.unblockSender(entry.id), `${entry.number} is no longer blocked.`); }}>
                      Unblock
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <Typography variant="subtitle1" gutterBottom>Calls turned away</Typography>
      <TableContainer component={Card} sx={{ mb: 3 }}>
        <Table size="small" aria-label="Calls turned away">
          <TableHead>
            <TableRow>
              <TableCell>When</TableCell>
              <TableCell>From</TableCell>
              <TableCell>To your number</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {(view?.rejections ?? []).length === 0 && (
              <TableRow><TableCell colSpan={3}>No calls turned away yet.</TableCell></TableRow>
            )}
            {(view?.rejections ?? []).map((call) => (
              <TableRow key={call.id}>
                <TableCell>{formatServerTime(call.rejected_at)}</TableCell>
                <TableCell>{call.number}</TableCell>
                <TableCell>{call.called ?? '-'}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      {past.length > 0 && (
        <>
          <Typography variant="subtitle1" gutterBottom>No longer blocked</Typography>
          {past.map((entry) => (
            <Typography key={entry.id} variant="body2" color="text.secondary">
              {entry.number}: {entry.removed_at
                ? `unblocked by ${entry.removed_by ?? 'an integration key'} on ${formatServerTime(entry.removed_at)}`
                : `block ended on ${formatServerTime(entry.expires_at)}`}. {entry.reason}
            </Typography>
          ))}
        </>
      )}
    </Box>
  );
}

export default BlockedSenders;
