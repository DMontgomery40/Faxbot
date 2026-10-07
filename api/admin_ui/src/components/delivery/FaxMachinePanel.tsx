import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, MenuItem, Stack, TextField, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../../api/client';
import type { FaxMachineView, IafServer } from '../../api/numbersTypes';
import { formatServerTime } from '../../api/time';

interface FaxMachinePanelProps {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}

function message(error: unknown, fallback: string) {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

// Recipients → Details, "Their fax machine": what this number's fax machine said on recent calls, what Faxbot
// learned from them (asking for fax over IP at once, the speed to start at), and Internet Aware Fax between fax
// servers, which you approve per number.
export default function FaxMachinePanel({ client, number, canWrite }: FaxMachinePanelProps) {
  const [view, setView] = useState<FaxMachineView | null>(null);
  const [server, setServer] = useState<IafServer | null>(null);
  const [kind, setKind] = useState<'peer' | 'endpoint'>('endpoint');
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const [result, servers] = await Promise.all([client.getFaxMachine(number), client.listIafServers()]);
      if (!alive.current) return;
      setView(result);
      setServer(servers.servers.find((item) => item.number === result.number) ?? null);
    } catch (error) {
      if (alive.current && !isForbidden(error)) setView(null);
    }
  }, [client, number]);
  useEffect(() => { void load(); }, [load]);

  const run = async (action: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setNotice(null);
    try {
      await action();
      if (!alive.current) return;
      setNotice({ severity: 'success', text: done });
      setLabel('');
      await load();
    } catch (error) {
      if (alive.current) setNotice({ severity: 'error', text: message(error, 'Nothing was changed. Try again.') });
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  if (!view) return null;
  const latest = view.calls[0];
  return (
    <Box data-testid="fax-machine" sx={{ mt: 3 }}>
      <Typography variant="subtitle1">Their fax machine</Typography>
      <Typography variant="body2" color="text.secondary">{view.sentence}</Typography>
      {latest && (
        <Box sx={{ mt: 1 }}>
          <Typography variant="body2" color="text.secondary">Last call, {formatServerTime(latest.when)}:</Typography>
          {latest.sentences.map((text) => <Typography key={text} variant="body2">{text}</Typography>)}
        </Box>
      )}
      {view.learned.sentences.map((text) => <Alert key={text} severity="info" sx={{ mt: 1 }}>{text}</Alert>)}
      {notice && <Alert severity={notice.severity} sx={{ mt: 1 }}>{notice.text}</Alert>}

      <Typography variant="subtitle2" sx={{ mt: 2 }}>Internet Aware Fax (fax between fax servers, faster than a phone line)</Typography>
      {view.iaf ? (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}>
          <Typography variant="body2">
            {server
              ? `Faxes to and from this number go as Internet Aware Fax: ${server.label}.`
              : 'Faxes to and from this number go as Internet Aware Fax: it is a partner office marked for it.'}
          </Typography>
          {server && canWrite && (
            <Button size="small" disabled={busy} onClick={() => { void run(() => client.removeIaf(server.id), 'Faxes to this number go at fax line speed again.'); }}>
              Stop
            </Button>
          )}
        </Stack>
      ) : (
        <>
          <Typography variant="body2" color="text.secondary">
            Only for a fax server that receives over the internet and takes Internet Aware Fax, such as another
            Faxbot or a Brooktrout SR140. Faxes then go faster than a fax line, so calls are shorter. Never use it for
            a fax machine on a phone line.
          </Typography>
          {canWrite && (
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'flex-start' }} sx={{ mt: 1 }}>
              <TextField select size="small" label="This number is" value={kind} sx={{ minWidth: 200 }}
                onChange={(event) => setKind(event.target.value as 'peer' | 'endpoint')}>
                <MenuItem value="peer">Another Faxbot</MenuItem>
                <MenuItem value="endpoint">A fax server that takes Internet Aware Fax</MenuItem>
              </TextField>
              <TextField size="small" label="Name" value={label} onChange={(event) => setLabel(event.target.value)}
                placeholder="Head office SR140" />
              <Button variant="outlined" disabled={busy || !label.trim()}
                onClick={() => { void run(() => client.approveIaf({ number: view.number, kind, label: label.trim() }), 'Faxes to and from this number now go as Internet Aware Fax.'); }}>
                Use Internet Aware Fax
              </Button>
            </Stack>
          )}
        </>
      )}
    </Box>
  );
}
