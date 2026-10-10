// The station check (routing/stations.py): what Faxbot does when a number answers as another fax machine, per
// recipient (Recipients → Details) and per mailbox (Numbers → Sender identity), and what Sent details say.
import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, MenuItem, Stack, TextField, Typography,
} from '@mui/material';
import type AdminAPIClient from '../api/client';
import { AdminAPIError } from '../api/client';

type Client = Pick<AdminAPIClient, 'call'>;
type Mode = 'warn' | 'refuse';

interface StationsView {
  number: string;
  mode: Mode;
  mode_source: 'recipient' | 'mailbox' | 'default';
  stations: Array<{ station: string; source: 'call' | 'person'; seen_at: string | null; actor_name: string | null }>;
  sentence?: string;
}

const MODE_LABELS: Record<Mode, string> = {
  warn: 'Send anyway and say so in Sent',
  refuse: 'Hang up before any page',
};

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

function read<T>(client: Client, target: string): Promise<T> {
  try {
    return client.call<T>({ method: 'GET', path: target });
  } catch (failure) {
    return Promise.reject(failure);
  }
}

const path = (value: string) => encodeURIComponent(value);

// Recipients → Details.
export function RecipientStationCheck({ client, number, canWrite }: { client: Client; number: string; canWrite: boolean }) {
  const [view, setView] = useState<StationsView | null>(null);
  const [station, setStation] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    read<StationsView>(client, `/routing/stations/${path(number)}`)
      .then((found) => { if (live) setView(found); })
      .catch(() => { if (live) setView(null); });
    return () => { live = false; };
  }, [client, number]);
  if (!view) return null;
  const save = async (body: { mode?: Mode; station?: string }) => {
    try {
      const result = await client.call<StationsView>({ method: 'PUT', path: `/routing/stations/${path(number)}`, body });
      setView(result);
      setMessage(result.sentence ?? null);
      return true;
    } catch (failure) {
      setMessage(problem(failure, 'This could not be saved. Try again.'));
      return false;
    }
  };
  return (
    <Box sx={{ mt: 2 }} role="region" aria-label="Station check">
      <Typography variant="subtitle2">When this number answers as another fax machine</Typography>
      <TextField select size="small" fullWidth margin="dense" label="What Faxbot does" value={view.mode} disabled={!canWrite}
        onChange={(event) => void save({ mode: event.target.value as Mode })}
        helperText={view.mode_source === 'mailbox' ? 'Set by the mailbox the fax is sent from, until you choose here.'
          : 'Faxbot compares the fax number the machine shows with this number and the ones it showed before.'}>
        {(Object.keys(MODE_LABELS) as Mode[]).map((mode) => <MenuItem key={mode} value={mode}>{MODE_LABELS[mode]}</MenuItem>)}
      </TextField>
      {view.stations.length > 0 && (
        <Typography variant="body2" color="text.secondary">
          Also expected: {view.stations.map((item) => item.station).join(', ')}
        </Typography>
      )}
      {canWrite && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1 }}>
          <TextField size="small" label="Another fax number its machine shows" value={station}
            onChange={(event) => setStation(event.target.value)} />
          <Button size="small" disabled={!station.trim()}
            onClick={() => void save({ station: station.trim() }).then((done) => { if (done) setStation(''); })}>Add</Button>
        </Stack>
      )}
      {message && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{message}</Typography>}
    </Box>
  );
}

// Numbers → Sender identity: the same choice for one mailbox's faxes.
export function MailboxStationCheck({ client, canWrite, mailboxes }: {
  client: Client; canWrite: boolean; mailboxes: Array<{ id: string; label: string }>;
}) {
  const [mailboxId, setMailboxId] = useState('');
  const [mode, setMode] = useState<Mode>('refuse');
  const [message, setMessage] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  if (!canWrite || mailboxes.length === 0) return null;
  const save = async () => {
    try {
      const result = await client.call<{ sentence: string }>({
        method: 'PUT', path: `/routing/stations/mailboxes/${path(mailboxId)}`, body: { mode } });
      setMessage({ severity: 'success', text: result.sentence });
    } catch (failure) {
      setMessage({ severity: 'error', text: problem(failure, 'This could not be saved. Try again.') });
    }
  };
  return (
    <Card variant="outlined" sx={{ mt: 3 }} role="region" aria-label="Station check for mailboxes">
      <CardContent>
        <Typography variant="h6" component="h2">When a number answers as another fax machine</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Before any page, Faxbot compares the fax number the answering machine shows with the number dialled and the
          ones it showed before. By default the fax goes on and Sent says so. A recipient's own choice comes first.
        </Typography>
        {message && <Alert severity={message.severity} sx={{ mb: 2 }} onClose={() => setMessage(null)}>{message.text}</Alert>}
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}>
          <TextField select size="small" label="Mailbox" value={mailboxId} sx={{ minWidth: 180 }}
            onChange={(event) => setMailboxId(event.target.value)}>
            {mailboxes.map((box) => <MenuItem key={box.id} value={box.id}>{box.label}</MenuItem>)}
          </TextField>
          <TextField select size="small" label="What Faxbot does" value={mode} sx={{ minWidth: 260 }}
            onChange={(event) => setMode(event.target.value as Mode)}>
            {(Object.keys(MODE_LABELS) as Mode[]).map((item) => <MenuItem key={item} value={item}>{MODE_LABELS[item]}</MenuItem>)}
          </TextField>
          <Button variant="outlined" disabled={!mailboxId} onClick={() => void save()}>Save</Button>
        </Stack>
      </CardContent>
    </Card>
  );
}

// Sent details: the stations this fax's calls answered as, when they were not the ones Faxbot expected.
export function SentStationCheck({ client, jobId }: { client: Client; jobId: string }) {
  const [sentences, setSentences] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    read<{ sentences: string[] }>(client, `/routing/stations/faxes/${path(jobId)}`)
      .then((found) => { if (live) setSentences(found.sentences ?? []); })
      .catch(() => { if (live) setSentences([]); });
    return () => { live = false; };
  }, [client, jobId]);
  if (sentences.length === 0) return null;
  return (
    <Alert severity="warning" sx={{ mt: 2 }} data-testid="sent-station-check">
      {sentences.map((sentence) => <Typography key={sentence} variant="body2">{sentence}</Typography>)}
    </Alert>
  );
}
