// The header notice (header_notice.py): a notice line printed at the top of every page you send, for the
// organization and per mailbox (Numbers → Sender identity); the "first page is a cover sheet" choice on Send a
// fax; whether a recipient needs a cover sheet (Recipients); and what Sent details say about a fax's notice.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Checkbox, FormControlLabel, MenuItem, Stack, Switch, TextField, Typography,
} from '@mui/material';
import type AdminAPIClient from '../api/client';
import { AdminAPIError, isForbidden } from '../api/client';

type Client = Pick<AdminAPIClient, 'call'>;

export interface NoticeItem { notice: string; actor_name: string | null; changed_at: string | null }
export interface NoticeSettings {
  organization: NoticeItem | null;
  mailboxes: Array<NoticeItem & { mailbox_id: string; mailbox: string }>;
  max_length: number;
  sentence?: string;
}

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

const path = (value: string) => encodeURIComponent(value);

// A read that never throws while rendering: a client without the request (an older server) reads as nothing.
function read<T>(client: Client, target: string): Promise<T> {
  try {
    return client.call<T>({ method: 'GET', path: target });
  } catch (failure) {
    return Promise.reject(failure);
  }
}

// Numbers → Sender identity, Header notice.
export function HeaderNoticeSettings({ client, canWrite, mailboxes }: {
  client: Client; canWrite: boolean; mailboxes: Array<{ id: string; label: string }>;
}) {
  const [view, setView] = useState<NoticeSettings | null>(null);
  const [hidden, setHidden] = useState(false);
  const [text, setText] = useState('');
  const [mailboxId, setMailboxId] = useState('');
  const [mailboxText, setMailboxText] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);

  const load = useCallback(() => {
    read<NoticeSettings>(client, '/header-notice').then((found) => {
      setView(found);
      setText(found.organization?.notice ?? '');
    }).catch((failure) => {
      if (isForbidden(failure)) setHidden(true);
      else setMessage({ severity: 'error', text: problem(failure, 'Header notices could not be read. Try again.') });
    });
  }, [client]);
  useEffect(() => { load(); }, [load]);

  const save = async (target: string, notice: string) => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await client.call<NoticeSettings>({ method: 'PUT', path: target, body: { notice } });
      setView(result);
      setText(result.organization?.notice ?? '');
      setMessage({ severity: 'success', text: result.sentence ?? 'Saved.' });
      return true;
    } catch (failure) {
      setMessage({ severity: 'error', text: problem(failure, 'The notice could not be saved. Try again.') });
      return false;
    } finally {
      setBusy(false);
    }
  };

  if (hidden) return null;
  const max = view?.max_length ?? 120;
  return (
    <Card variant="outlined" sx={{ mt: 3 }} aria-label="Header notice" role="region">
      <CardContent>
        <Typography variant="h6" component="h2">Header notice</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          One line, such as a confidentiality notice, printed at the top of every page you send, under the header line
          with your name and reply number. When it is set, a sender can mark a fax's first page as a cover sheet whose
          notice goes here instead, so that page is not sent.
        </Typography>
        {message && <Alert severity={message.severity} sx={{ mb: 2 }} onClose={() => setMessage(null)}>{message.text}</Alert>}
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'flex-start' }}>
          <TextField fullWidth size="small" label="Notice on every page" value={text} disabled={!canWrite || busy}
            onChange={(event) => setText(event.target.value)} inputProps={{ maxLength: max }}
            helperText={`${text.length} of ${max} characters`} />
          {canWrite && <Button variant="outlined" disabled={busy || !text.trim()}
            onClick={() => void save('/header-notice', text)}>Save</Button>}
          {canWrite && view?.organization && <Button disabled={busy}
            onClick={() => void save('/header-notice', '')}>Remove</Button>}
        </Stack>
        <Box sx={{ mt: 2 }}>
          <Typography variant="subtitle2">Mailboxes with their own notice</Typography>
          {view && view.mailboxes.length === 0 && (
            <Typography variant="body2" color="text.secondary">None. Every mailbox's faxes carry the notice above.</Typography>
          )}
          {view?.mailboxes.map((item) => (
            <Stack key={item.mailbox_id} direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mt: 1 }}>
              <Typography variant="body2">{item.mailbox}: {item.notice}</Typography>
              {canWrite && <Button size="small" disabled={busy}
                onClick={() => void save(`/header-notice/mailboxes/${path(item.mailbox_id)}`, '')}>Remove</Button>}
            </Stack>
          ))}
          {canWrite && mailboxes.length > 0 && (
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mt: 2 }}>
              <TextField select size="small" label="Mailbox" value={mailboxId} sx={{ minWidth: 180 }}
                onChange={(event) => setMailboxId(event.target.value)}>
                {mailboxes.map((box) => <MenuItem key={box.id} value={box.id}>{box.label}</MenuItem>)}
              </TextField>
              <TextField size="small" label="Its notice" value={mailboxText} sx={{ flexGrow: 1 }}
                onChange={(event) => setMailboxText(event.target.value)} inputProps={{ maxLength: max }} />
              <Button variant="outlined" disabled={busy || !mailboxId || !mailboxText.trim()}
                onClick={() => void save(`/header-notice/mailboxes/${path(mailboxId)}`, mailboxText)
                  .then((done) => { if (done) setMailboxText(''); })}>Add</Button>
            </Stack>
          )}
        </Box>
      </CardContent>
    </Card>
  );
}

// Send a fax: "the first page is a cover sheet", offered only when a notice applies to the fax.
export function CoverInHeaderChoice({ client, mailbox, checked, onChange, disabled }: {
  client: Client; mailbox: string | null; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const query = mailbox ? `?mailbox=${path(mailbox)}` : '';
    read<{ notice: string | null }>(client, `/header-notice/for-send${query}`)
      .then((found) => { if (live) setNotice(found.notice); })
      .catch(() => { if (live) setNotice(null); });
    return () => { live = false; };
  }, [client, mailbox]);
  useEffect(() => { if (notice === null && checked) onChange(false); }, [notice, checked, onChange]);
  if (!notice) return null;
  return (
    <Box>
      <FormControlLabel data-testid="send-cover-in-header"
        control={<Checkbox checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />}
        label="The first page is a cover sheet: print its notice at the top of every page instead and leave it out" />
      <Typography variant="caption" color="text.secondary" display="block" sx={{ ml: 4 }}>
        Every page carries: {notice}
      </Typography>
    </Box>
  );
}

// Recipients → Details: whether this recipient needs a cover sheet.
export function RecipientCoverSwitch({ client, number, canWrite }: { client: Client; number: string; canWrite: boolean }) {
  const [needs, setNeeds] = useState<boolean | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    read<{ needs_cover: boolean }>(client, `/header-notice/recipients/${path(number)}`)
      .then((found) => { if (live) setNeeds(found.needs_cover); })
      .catch(() => { if (live) setNeeds(null); });
    return () => { live = false; };
  }, [client, number]);
  if (needs === null) return null;
  const change = async (value: boolean) => {
    try {
      const result = await client.call<{ needs_cover: boolean; sentence: string }>({
        method: 'PUT', path: `/header-notice/recipients/${path(number)}`, body: { needs_cover: value } });
      setNeeds(result.needs_cover);
      setMessage(result.sentence);
    } catch (failure) {
      setMessage(problem(failure, 'This could not be saved. Try again.'));
    }
  };
  return (
    <Box sx={{ mt: 1 }}>
      <FormControlLabel
        control={<Switch checked={needs} disabled={!canWrite} onChange={(event) => void change(event.target.checked)}
          inputProps={{ 'aria-label': 'Needs a cover sheet' }} />}
        label="Needs a cover sheet: always send the cover, even when the sender puts its notice in the header" />
      {message && <Typography variant="body2" color="text.secondary">{message}</Typography>}
    </Box>
  );
}

// Sent details: what happened to a fax's header notice and cover sheet.
export function SentHeaderNotice({ client, jobId }: { client: Client; jobId: string }) {
  const [view, setView] = useState<{ notice: string | null; sentence: string | null } | null>(null);
  useEffect(() => {
    let live = true;
    read<{ notice: string | null; sentence: string | null }>(client, `/header-notice/faxes/${path(jobId)}`)
      .then((found) => { if (live) setView(found); })
      .catch(() => { if (live) setView(null); });
    return () => { live = false; };
  }, [client, jobId]);
  if (!view?.sentence) return null;
  return (
    <Alert severity="info" sx={{ mt: 2 }} data-testid="sent-header-notice">
      <Typography variant="body2">{view.sentence}</Typography>
      {view.notice && <Typography variant="caption" display="block">Notice: {view.notice}</Typography>}
    </Alert>
  );
}
