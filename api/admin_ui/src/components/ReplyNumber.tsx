import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Link, MenuItem, Stack, TextField, Typography,
} from '@mui/material';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../api/client';
import type { AccessMailbox } from '../api/types';
import type { ReplyNumberView } from '../api/numbersTypes';
import { HeaderNoticeSettings } from './HeaderNotice';
import { MailboxStationCheck } from './StationCheck';

interface ReplyNumberProps {
  client: AdminAPIClient;
  canWrite: boolean;
}

function message(error: unknown, fallback: string) {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

// Numbers → Sender identity, Reply number: the number printed in each page's header line and sent as the
// station ID, for every fax (setting fax_reply_number) and per mailbox (setting fax_reply_numbers); which of
// your numbers is cheapest to receive on; and what caller ID each provider shows.
function ReplyNumber({ client, canWrite }: ReplyNumberProps) {
  const [view, setView] = useState<ReplyNumberView | null>(null);
  const [mailboxes, setMailboxes] = useState<AccessMailbox[]>([]);
  const [number, setNumber] = useState('');
  const [mailboxId, setMailboxId] = useState('');
  const [mailboxNumber, setMailboxNumber] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const result = await client.getReplyNumber();
      if (alive.current) {
        setView(result);
        setNumber(result.number ?? '');
      }
    } catch (error) {
      if (alive.current && !isForbidden(error)) setNotice({ severity: 'error', text: message(error, 'Faxbot could not read your numbers. Try again.') });
    }
    try {
      const page = await client.listMailboxes();
      if (alive.current) setMailboxes(page.items ?? []);
    } catch {
      if (alive.current) setMailboxes([]);
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
      await load();
    } catch (error) {
      if (alive.current) setNotice({ severity: 'error', text: message(error, 'The reply number was not saved. Try again.') });
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  if (!view) return notice ? <Alert severity={notice.severity}>{notice.text}</Alert> : null;
  const reaching = view.candidates.filter((item) => item.mailbox_id === mailboxId && item.receives);
  return (
    <>
    <Card data-testid="reply-number" sx={{ mt: 3 }}>
      <CardContent>
        <Typography variant="h6">Reply number</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          The number printed at the top of each page you send and shown on the receiving fax machine. Replies go there.
        </Typography>
        <Alert severity="info" sx={{ mb: 1 }}>{view.sentence}</Alert>
        {view.problems.map((text) => <Alert key={text} severity="warning" sx={{ mb: 1 }}>{text}</Alert>)}
        {view.header_problem && <Alert severity="warning" sx={{ mb: 1 }}>{view.header_problem}</Alert>}
        {notice && <Alert severity={notice.severity} sx={{ mb: 1 }}>{notice.text}</Alert>}

        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mt: 2 }}>
          <TextField size="small" label="Reply number for every fax" value={number} disabled={!canWrite || busy}
            placeholder="Faxbot chooses" onChange={(event) => setNumber(event.target.value)}
            helperText="One of your numbers whose faxes reach a mailbox." />
          <Button variant="contained" disabled={!canWrite || busy || !number.trim()}
            onClick={() => { void run(() => client.setReplyNumber(number.trim()), 'Saved. New faxes show this number.'); }}>
            Save
          </Button>
          <Button disabled={!canWrite || busy || !view.number}
            onClick={() => { void run(() => client.setReplyNumber(''), 'Faxbot chooses the number again.'); }}>
            Let Faxbot choose
          </Button>
        </Stack>

        {view.suggestion && (
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mt: 2 }}>
            <Typography variant="body2">{view.suggestion.sentence}</Typography>
            {view.number !== view.suggestion.number && (
              <Button size="small" variant="outlined" disabled={!canWrite || busy}
                onClick={() => { void run(() => client.setReplyNumber(view.suggestion!.number), 'Saved. New faxes show this number.'); }}>
                Use this number
              </Button>
            )}
          </Stack>
        )}
        {view.avoid.map((item) => <Alert key={item.number} severity="warning" sx={{ mt: 1 }}>{item.sentence}</Alert>)}

        {view.caller_id.length > 0 && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2">Caller ID</Typography>
            {view.caller_id.map((item) => (
              <Typography key={item.provider} variant="body2" sx={{ mt: 0.5 }}>
                {item.sentence}{' '}
                {item.source_url && <Link href={item.source_url} target="_blank" rel="noreferrer">{item.provider}'s rule</Link>}
              </Typography>
            ))}
          </Box>
        )}

        <Box sx={{ mt: 3 }}>
          <Typography variant="subtitle2">Mailboxes with their own reply number</Typography>
          {view.mailboxes.length === 0 && (
            <Typography variant="body2" color="text.secondary">None. Every mailbox's faxes show the number above.</Typography>
          )}
          {view.mailboxes.map((item) => (
            <Stack key={item.mailbox_id} direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mt: 1 }}>
              <Typography variant="body2">{item.mailbox}: {item.number}</Typography>
              {item.problem && <Typography variant="body2" color="warning.main">{item.problem}</Typography>}
              <Button size="small" disabled={!canWrite || busy}
                onClick={() => { void run(() => client.clearMailboxReplyNumber(item.mailbox_id), `Faxes from ${item.mailbox} show the number above again.`); }}>
                Remove
              </Button>
            </Stack>
          ))}
          {canWrite && mailboxes.length > 0 && (
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mt: 2 }}>
              <TextField select size="small" label="Mailbox" value={mailboxId} sx={{ minWidth: 180 }}
                onChange={(event) => { setMailboxId(event.target.value); setMailboxNumber(''); }}>
                {mailboxes.map((box) => <MenuItem key={box.id} value={box.id}>{box.label}</MenuItem>)}
              </TextField>
              <TextField select size="small" label="Its reply number" value={mailboxNumber} sx={{ minWidth: 200 }}
                disabled={!mailboxId || reaching.length === 0}
                helperText={mailboxId && reaching.length === 0 ? 'None of your numbers reaches this mailbox yet.' : ' '}
                onChange={(event) => setMailboxNumber(event.target.value)}>
                {reaching.map((item) => <MenuItem key={item.number} value={item.number}>{item.number}</MenuItem>)}
              </TextField>
              <Button variant="outlined" disabled={busy || !mailboxId || !mailboxNumber}
                onClick={() => { void run(() => client.setMailboxReplyNumber(mailboxId, mailboxNumber), 'Saved. Faxes from that mailbox show this number.'); }}>
                Add
              </Button>
            </Stack>
          )}
        </Box>
      </CardContent>
    </Card>
    {/* The notice line printed under the header line on every page (header_notice.py). */}
    <HeaderNoticeSettings client={client} canWrite={canWrite} mailboxes={mailboxes} />
    {/* What a mailbox's faxes do when a number answers as another fax machine (routing/stations.py). */}
    <MailboxStationCheck client={client} canWrite={canWrite} mailboxes={mailboxes} />
    </>
  );
}

export default ReplyNumber;
