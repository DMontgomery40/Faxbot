// The caller-verification stamp on received faxes (inbound/caller_check.py): Received shows what the network
// asserted about who called; the trunk page keeps your registered senders for received faxes.
import { useEffect, useState } from 'react';
import { Alert, Box, Button, Popover, Stack, TextField, Typography } from '@mui/material';
import { AdminAPIError } from '../api/client';

type Call = <T>(request: { method: string; path: string; body?: unknown }) => Promise<T>;
interface Stamp { stamp: 'verified_registered' | 'verified_unregistered' | 'unverified'; sentence: string }

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

// Received: one button per fax that opens what the network asserted about the caller.
export function ReceivedCallerCheck({ call, inboundId }: { call: Call; inboundId: string }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [text, setText] = useState<string | null>(null);
  const open = async (target: HTMLElement) => {
    setAnchor(target);
    try {
      const found = await call<{ stamp: Stamp | null }>({ method: 'GET', path: `/caller-check/faxes/${encodeURIComponent(inboundId)}` });
      setText(found.stamp?.sentence ?? 'Faxbot kept no caller check for this fax: the network said nothing about the caller.');
    } catch (failure) {
      setText(problem(failure, 'The caller check could not be loaded. Try again.'));
    }
  };
  return (
    <>
      <Button size="small" onClick={(event) => void open(event.currentTarget)}>Who called</Button>
      <Popover open={!!anchor} anchorEl={anchor} onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}>
        <Typography variant="body2" sx={{ p: 2, maxWidth: 360 }}>{text ?? 'Loading…'}</Typography>
      </Popover>
    </>
  );
}

// Providers → the trunk page: registered senders for received faxes (not the sending side's registered senders).
export function ReceivedRegisteredSenders({ call }: { call: Call }) {
  const [numbers, setNumbers] = useState<string | null>(null);
  const [sentence, setSentence] = useState('');
  const [message, setMessage] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  useEffect(() => {
    let live = true;
    call<{ numbers: string[]; sentence: string }>({ method: 'GET', path: '/caller-check/registered' })
      .then((found) => { if (live) { setNumbers(found.numbers.join(', ')); setSentence(found.sentence); } })
      .catch(() => { if (live) setNumbers(null); });
    return () => { live = false; };
  }, [call]);
  if (numbers === null) return null;
  const save = async () => {
    try {
      const result = await call<{ numbers: string[]; sentence: string; saved: string }>({
        method: 'PUT', path: '/caller-check/registered',
        body: { numbers: numbers.split(',').map((item) => item.trim()).filter(Boolean) } });
      setNumbers(result.numbers.join(', '));
      setSentence(result.sentence);
      setMessage({ severity: 'success', text: result.saved });
    } catch (failure) {
      setMessage({ severity: 'error', text: problem(failure, 'This could not be saved. Try again.') });
    }
  };
  return (
    <Box role="region" aria-label="Registered senders for received faxes">
      <Typography variant="subtitle2">Who called: registered senders for received faxes</Typography>
      <Typography variant="body2" color="text.secondary">
        When your carrier passes its STIR/SHAKEN result, each received fax says whether the network verified the caller
        number and whether it is one of these. It shows who placed the call, never that the document is genuine. {sentence}
      </Typography>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }} sx={{ mt: 1 }}>
        <TextField size="small" fullWidth label="Registered senders" value={numbers} placeholder="+13035550150, +13035550151"
          onChange={(event) => setNumbers(event.target.value)} />
        <Button variant="outlined" onClick={() => void save()}>Save</Button>
      </Stack>
      {message && <Alert severity={message.severity} sx={{ mt: 1 }} onClose={() => setMessage(null)}>{message.text}</Alert>}
    </Box>
  );
}
