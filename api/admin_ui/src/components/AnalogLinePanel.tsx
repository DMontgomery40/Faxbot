// Providers → the trunk page, for an analog line through a gateway (routing/analog.py): its local calling area and
// prices, so local numbers go out on it at no extra cost. Shows nothing for a trunk that is not an analog line.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Card, CardContent, Link, Stack, TextField, Typography } from '@mui/material';
import { AdminAPIError } from '../api/client';
import { formatLocalDate } from '../api/time';

type Call = <T>(request: { method: string; path: string; body?: unknown }) => Promise<T>;

interface AnalogLineView {
  analog: boolean;
  account: string;
  label?: string;
  preset_label?: string;
  calls_at_once?: number;
  local_prefixes?: number;
  imported_at?: string | null;
  sources?: string[];
  toll_rate?: string | null;
  monthly_fee?: string | null;
  sentence?: string;
  routed?: boolean;
  route_sentence?: string | null;
  saved?: string;
  save_page_help?: string;
}

// The chosen file's text (FileReader works in every browser the console supports).
function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

// ``expectAnalog``: the trunk is known to be an analog line, so a failed read is said; otherwise it shows nothing.
export default function AnalogLinePanel({ call, accountKey, canWrite = true, expectAnalog = false }: {
  call: Call; accountKey: string; canWrite?: boolean; expectAnalog?: boolean;
}) {
  const [view, setView] = useState<AnalogLineView | null>(null);
  const [failed, setFailed] = useState(false);
  const [file, setFile] = useState<{ name: string; text: string } | null>(null);
  const [plan, setPlan] = useState('');
  const [line, setLine] = useState('');
  const [toll, setToll] = useState('');
  const [fee, setFee] = useState('');
  const [source, setSource] = useState('');
  const [message, setMessage] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setView(await call<AnalogLineView>({ method: 'GET', path: `/routing/analog-lines/${encodeURIComponent(accountKey)}` }));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [call, accountKey]);
  useEffect(() => { void load(); }, [load]);

  if (failed) {
    if (!expectAnalog) return null;
    return <Typography variant="body2" color="text.secondary">This line's local calling area could not be loaded. Open the trunk again to try again.</Typography>;
  }
  if (!view || !view.analog) return null;

  const choose = async (picked: File | undefined) => {
    if (!picked) return;
    if (picked.size > 4_000_000) {
      setMessage({ severity: 'error', text: 'This file is larger than 4 MB. Save only the list of local prefixes for your line.' });
      return;
    }
    try {
      setFile({ name: picked.name, text: await readText(picked) });
    } catch {
      setMessage({ severity: 'error', text: 'This file could not be read. Choose it again.' });
    }
  };
  const save = async () => {
    if (!file) return;
    setSaving(true);
    try {
      const body: Record<string, string> = { text: file.text, filename: file.name };
      for (const [key, value] of [['plan', plan], ['line', line], ['toll_per_minute', toll], ['monthly_fee', fee], ['source_url', source]]) {
        if (value.trim()) body[key] = value.trim();
      }
      const result = await call<AnalogLineView>({ method: 'PUT', path: `/routing/analog-lines/${encodeURIComponent(accountKey)}/local-calls`, body });
      setView(result);
      setFile(null);
      setMessage({ severity: 'success', text: result.saved ?? result.sentence ?? 'Saved.' });
    } catch (failure) {
      setMessage({ severity: 'error', text: problem(failure, 'The list could not be saved. Try again.') });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card variant="outlined" role="region" aria-label="Local calls on this line">
      <CardContent>
        <Typography variant="h6" component="h2">Local calls on this line</Typography>
        <Typography variant="body2" sx={{ mb: 1 }}>{view.sentence}</Typography>
        {view.route_sentence && <Alert severity="warning" sx={{ mb: 1 }}>{view.route_sentence}</Alert>}
        <Typography variant="body2" color="text.secondary">
          {`${view.calls_at_once === 1 ? 'One call at a time' : `${view.calls_at_once} calls at once`}`}
          {view.monthly_fee ? `; ${view.monthly_fee} a month` : ''}
          {view.imported_at ? `; list saved ${formatLocalDate(view.imported_at)}` : ''}.
        </Typography>
        {(view.sources ?? []).map((url) => (
          <Typography key={url} variant="body2"><Link href={url} target="_blank" rel="noreferrer">Where the list came from</Link></Typography>
        ))}
        {canWrite && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="body2" sx={{ mb: 1 }}>
              Save your line's Local prefixes page from the Local Calling Guide as a web page, or a list from your carrier
              with one prefix a line, such as 303-426, and choose it here. Faxbot never looks the list up itself.
              {view.save_page_help && <> <Link href={view.save_page_help} target="_blank" rel="noreferrer">How the Local Calling Guide says to save it</Link></>}
            </Typography>
            <Stack spacing={1.5}>
              <Button variant="outlined" component="label" sx={{ alignSelf: 'flex-start' }}>
                {file ? `List: ${file.name}` : 'Choose the saved list'}
                <input hidden type="file" accept=".html,.htm,.xml,.csv,.txt,text/html,text/plain,text/csv,application/xml"
                  aria-label="Saved list of local prefixes" onChange={(event) => void choose(event.target.files?.[0])} />
              </Button>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <TextField size="small" label="Your line's prefix (optional)" placeholder="303-426" value={line}
                  onChange={(event) => setLine(event.target.value)} helperText="Faxbot checks it is in the list." />
                <TextField size="small" label="Calling plan (optional)" value={plan} onChange={(event) => setPlan(event.target.value)}
                  helperText="Only when the list names several plans." />
              </Stack>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <TextField size="small" label="Other numbers, per minute" placeholder="0.10" value={toll}
                  onChange={(event) => setToll(event.target.value)} inputProps={{ inputMode: 'decimal' }}
                  helperText={view.toll_rate ? `Now ${view.toll_rate}. Leave empty to keep it.` : 'From your phone bill; 0 when your plan includes them.'} />
                <TextField size="small" label="Monthly fee (optional)" placeholder="45" value={fee}
                  onChange={(event) => setFee(event.target.value)} inputProps={{ inputMode: 'decimal' }} />
              </Stack>
              <TextField size="small" label="Where the list came from (optional)" placeholder="https://www.localcallingguide.com/..."
                value={source} onChange={(event) => setSource(event.target.value)} />
              <Button variant="contained" sx={{ alignSelf: 'flex-start' }} disabled={!file || saving} onClick={() => void save()}>
                Save the local calling area
              </Button>
            </Stack>
          </Box>
        )}
        {message && <Alert severity={message.severity} sx={{ mt: 2 }} onClose={() => setMessage(null)}>{message.text}</Alert>}
      </CardContent>
    </Card>
  );
}
