// Providers → the trunk page → Send-only numbers: numbers you show on faxes you send but never receive on here,
// such as your main office number, and the numbers you rent only to send from.
// Saved as the fax_send_only_numbers setting through PUT /admin/sip/send-only, which refuses a number an account
// receives on.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, IconButton, Link, Paper, Stack, TextField, Typography } from '@mui/material';
import { Delete } from '@mui/icons-material';

type Call = <T>(request: { method: string; path: string; body?: unknown }) => Promise<T>;

export interface SendOnlyView {
  numbers: Array<{
    number: string;
    sentence: string;
    carrier_rules: Array<{ trunk: string; sentence: string; source_url: string; read_on: string }>;
  }>;
  advice: Array<{ number: string; sentence: string; source_url: string | null }>;
  quiet_days: number;
}

export default function SendOnlyNumbers({ call }: { call: Call }) {
  const [view, setView] = useState<SendOnlyView | null>(null);
  const [entry, setEntry] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setView(await call<SendOnlyView>({ method: 'GET', path: '/admin/sip/send-only' }));
    } catch {
      setProblem('Faxbot could not read your send-only numbers just now.');
    }
  }, [call]);
  useEffect(() => { void load(); }, [load]);

  const save = async (numbers: string[]) => {
    setBusy(true);
    try {
      await call({ method: 'PUT', path: '/admin/sip/send-only', body: { numbers } });
      setProblem(null);
      setEntry('');
      await load();
    } catch (error) {
      setProblem((error as { detail?: string })?.detail ?? 'Faxbot could not save the send-only numbers.');
    } finally {
      setBusy(false);
    }
  };

  if (!view) return problem ? <Alert severity="warning">{problem}</Alert> : null;
  const current = view.numbers.map((item) => item.number);
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
      <Typography variant="subtitle1">Send-only numbers</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        A number you already have, such as your main office number, that faxes you send can show as caller ID and
        station ID. Faxbot never receives on it, so a fax to it is a real call. Set it as a trunk's caller ID or as
        the station ID to show it.
      </Typography>
      {problem && <Alert severity="warning" sx={{ mb: 1 }}>{problem}</Alert>}
      <Stack spacing={1}>
        {view.numbers.map((item) => (
          <Box key={item.number}>
            <Stack direction="row" alignItems="center" spacing={1}>
              <Typography sx={{ fontFamily: 'monospace' }}>{item.number}</Typography>
              <IconButton size="small" aria-label={`Remove ${item.number}`} disabled={busy}
                onClick={() => void save(current.filter((number) => number !== item.number))}>
                <Delete fontSize="small" />
              </IconButton>
            </Stack>
            <Typography variant="body2" color="text.secondary">{item.sentence}</Typography>
            {item.carrier_rules.map((rule) => (
              <Typography key={rule.trunk} variant="body2">
                {rule.trunk}: {rule.sentence} <Link href={rule.source_url} target="_blank" rel="noreferrer">Source</Link>
              </Typography>
            ))}
          </Box>
        ))}
        <Stack direction="row" spacing={1} alignItems="center">
          <TextField size="small" label="Add a send-only number" placeholder="+13035550142" value={entry}
            onChange={(event) => setEntry(event.target.value)} />
          <Button variant="outlined" disabled={busy || !entry.trim()} onClick={() => void save([...current, entry.trim()])}>
            Add send-only number
          </Button>
        </Stack>
        {view.advice.map((item) => (
          <Alert key={item.number} severity="info">{item.sentence}</Alert>
        ))}
      </Stack>
    </Paper>
  );
}
