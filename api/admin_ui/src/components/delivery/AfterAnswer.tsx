// Digits after answer (routing/after_answer.py): keys Faxbot presses once a recipient's phone menu answers, before
// the fax starts (Recipients → Details), and the keys each call pressed (Sent details).
import { useEffect, useState } from 'react';
import { Alert, Box, Button, Stack, TextField, Typography } from '@mui/material';
import type AdminAPIClient from '../../api/client';
import { AdminAPIError } from '../../api/client';

type Client = Pick<AdminAPIClient, 'call'>;

interface AfterAnswerView {
  number: string;
  digits: string | null;
  spoken: string | null;
  sentence: string;
}

const path = (value: string) => encodeURIComponent(value);

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

// Recipients → Details.
export function RecipientAfterAnswer({ client, number, canWrite }: { client: Client; number: string; canWrite: boolean }) {
  const [view, setView] = useState<AfterAnswerView | null>(null);
  const [failed, setFailed] = useState(false);
  const [keys, setKeys] = useState('');
  const [message, setMessage] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  useEffect(() => {
    let live = true;
    client.call<AfterAnswerView>({ method: 'GET', path: `/routing/after-answer/${path(number)}` })
      .then((found) => { if (live) { setView(found); setKeys(found.digits ?? ''); setFailed(false); } })
      .catch(() => { if (live) { setView(null); setFailed(true); } });
    return () => { live = false; };
  }, [client, number]);
  if (failed) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
        The keys Faxbot presses after this number answers could not be loaded. Close and open this number to try again.
      </Typography>
    );
  }
  if (!view) return null;
  const save = async (digits: string | null) => {
    try {
      const result = await client.call<AfterAnswerView & { saved: string }>({
        method: 'PUT', path: `/routing/after-answer/${path(number)}`, body: { digits } });
      setView(result);
      setKeys(result.digits ?? '');
      setMessage({ severity: 'success', text: result.saved });
    } catch (failure) {
      setMessage({ severity: 'error', text: problem(failure, 'This could not be saved. Try again.') });
    }
  };
  return (
    <Box sx={{ mt: 2 }} role="region" aria-label="Keys to press after answer">
      <Typography variant="subtitle2">Phone menu before the fax machine</Typography>
      <Typography variant="body2" color="text.secondary">{view.sentence}</Typography>
      {canWrite && (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mt: 1 }}>
          <TextField size="small" label="Keys to press after it answers" value={keys}
            onChange={(event) => setKeys(event.target.value)} inputProps={{ maxLength: 64 }}
            helperText="Such as 2, or 2w105. w is a short pause, W a one-second pause. Only calls over your trunk can press keys." />
          <Button size="small" variant="outlined" onClick={() => void save(keys.trim() || null)}>Save</Button>
          {view.digits && <Button size="small" onClick={() => void save(null)}>Press none</Button>}
        </Stack>
      )}
      {message && <Alert severity={message.severity} sx={{ mt: 1 }} onClose={() => setMessage(null)}>{message.text}</Alert>}
    </Box>
  );
}

// Sent details: which keys each of the fax's calls pressed after answer, and that those seconds are billed.
export function SentAfterAnswer({ client, jobId }: { client: Client; jobId: string }) {
  const [sentences, setSentences] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    client.call<{ sentences: string[] }>({ method: 'GET', path: `/routing/after-answer/faxes/${path(jobId)}` })
      .then((found) => { if (live) setSentences(found.sentences ?? []); })
      .catch(() => { if (live) setSentences([]); });
    return () => { live = false; };
  }, [client, jobId]);
  if (sentences.length === 0) return null;
  return (
    <Alert severity="info" sx={{ mt: 2 }} data-testid="sent-after-answer">
      {sentences.map((sentence, index) => <Typography key={index} variant="body2">{sentence}</Typography>)}
    </Alert>
  );
}
