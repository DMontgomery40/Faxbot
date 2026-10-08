// Encoded pages (experimental): the per-number opt-in in a fax number's Details and a received fax's decode
// result. A sent fax's encoded pages are its page line ("How the pages were sent"), chosen for each attempt
// with the other layouts. The server words every state sentence.
import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, FormControlLabel, Switch, TextField, Typography,
} from '@mui/material';
import type AdminAPIClient from '../../api/client';
import type { CodecNumber, CodecReceived } from '../../api/codecTypes';
import { formatServerTime } from '../../api/time';
import { DeliveryError } from './shared';

const STYLES = [
  { value: 'dense', label: 'Dense pages' },
  { value: 'picture', label: 'A picture of the first page' },
] as const;
const LEVELS = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
] as const;

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

// The per-number setting, shown in a fax number's Details. It saves on its own.
export function EncodedPagesPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<CodecNumber | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [agreed, setAgreed] = useState(false);
  const [style, setStyle] = useState<'dense' | 'picture'>('dense');
  const [fec, setFec] = useState<'low' | 'medium' | 'high'>('medium');
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  const show = (loaded: CodecNumber) => {
    setView(loaded);
    setEnabled(loaded.enabled);
    setAgreed(false);
    setStyle(loaded.style);
    setFec(loaded.fec);
    setKey('');
    setClearKey(false);
  };

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    setSaved(null);
    client.getCodecNumber(number).then((loaded) => { if (live) show(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const turningOn = enabled && !view.enabled;
  const keyValid = key === '' || (key.trim().length >= 8 && key.length <= 200);
  const changed = enabled !== view.enabled || (enabled && (style !== view.style || fec !== view.fec
    || key !== '' || clearKey));

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const result = enabled
        ? await client.saveCodecNumber(number, {
          enabled: true, recipient_agreed: agreed, style, fec, version: view.version, clear_key: clearKey,
          ...(key ? { shared_key: key } : {}),
        })
        : await client.turnOffCodecNumber(number);
      show(result);
      setSaved(result.state_sentence);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box mt={2} data-testid="encoded-pages">
      <Typography variant="subtitle2">Encoded pages (experimental)</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }}>{view.state_sentence}</Typography>
      <Typography variant="body2" color="text.secondary">{view.limits_text}</Typography>
      {view.agreement && (
        <Typography variant="body2" color="text.secondary">
          The recipient&apos;s agreement was recorded by {view.agreement.by} on {formatServerTime(view.agreement.at)}.
        </Typography>
      )}
      {view.has_key && (
        <Typography variant="body2" color="text.secondary">
          Documents are encrypted with the shared key whose fingerprint is {view.key_fingerprint}.
        </Typography>
      )}
      <DeliveryError error={error} onClose={() => setError(null)} />
      {saved && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setSaved(null)}>{saved}</Alert>}
      <FormControlLabel sx={{ mt: 1 }} disabled={!canWrite || busy}
        control={<Switch checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />}
        label="Send documents to this number as encoded pages when that costs less" />
      {turningOn && (
        <FormControlLabel disabled={!canWrite || busy}
          control={<Checkbox checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />}
          label={view.agreement_text} />
      )}
      {enabled && (
        <Box display="flex" gap={2} flexWrap="wrap" mt={1}>
          <TextField select size="small" label="Page style" value={style} SelectProps={{ native: true }}
            onChange={(e) => setStyle(e.target.value as 'dense' | 'picture')} disabled={!canWrite || busy}
            InputLabelProps={{ shrink: true }} sx={{ width: 240 }}>
            {STYLES.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </TextField>
          {style === 'picture' && (view.has_key || key !== '') && !clearKey && (
            <Typography variant="body2" color="text.secondary" sx={{ width: '100%' }}>
              With a shared key, the picture is a plain pattern instead of the first page, so the page shows nothing of the document.
            </Typography>
          )}
          <TextField select size="small" label="Error correction" value={fec} SelectProps={{ native: true }}
            onChange={(e) => setFec(e.target.value as 'low' | 'medium' | 'high')} disabled={!canWrite || busy}
            helperText="Higher survives a noisier line but carries less on each page."
            InputLabelProps={{ shrink: true }} sx={{ width: 240 }}>
            {LEVELS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </TextField>
          <TextField size="small" type="password" label="Shared key (optional)" value={key}
            onChange={(e) => setKey(e.target.value)} disabled={!canWrite || busy || clearKey}
            error={!keyValid} autoComplete="new-password"
            helperText="Encrypts each document with a key you and the recipient agreed outside fax, 8 characters or more. Only its fingerprint is shown after saving."
            sx={{ width: 360 }} />
          {view.has_key && (
            <FormControlLabel disabled={!canWrite || busy}
              control={<Checkbox checked={clearKey} onChange={(e) => setClearKey(e.target.checked)} />}
              label="Stop encrypting with the shared key" />
          )}
        </Box>
      )}
      {canWrite && (
        <Box mt={1}>
          <Button size="small" variant="outlined" onClick={() => void save()}
            disabled={busy || !changed || !keyValid || (turningOn && !agreed)}>
            {enabled ? 'Save encoded pages' : 'Turn off encoded pages'}
          </Button>
        </Box>
      )}
    </Box>
  );
}

// A received fax's details: the decode result, and the decoded original to download.
export function ReceivedEncodedPages({ client, faxId, canDownload }: {
  client: AdminAPIClient;
  faxId: string | null | undefined;
  canDownload: boolean;
}) {
  const [view, setView] = useState<CodecReceived | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let live = true;
    setView(null);
    if (!faxId) return undefined;
    client.getCodecReceived(faxId).then((value) => { if (live) setView(value); }).catch(() => undefined);
    return () => { live = false; };
  }, [client, faxId]);
  if (!faxId || !view?.sentence) return null;
  const download = async () => {
    try {
      const name = view.document_name || (view.content_type === 'text/plain' ? 'decoded.txt' : 'decoded.pdf');
      saveBlob(await client.downloadDecodedDocument(faxId), name);
    } catch (failure) {
      setError(failure);
    }
  };
  return (
    <Box my={1} data-testid="received-encoded-pages">
      <Typography variant="caption" color="text.secondary">Encoded pages</Typography>
      <Typography variant="body2">{view.sentence}</Typography>
      {view.state === 'decoded' && canDownload && (
        <Button size="small" variant="outlined" sx={{ mt: 0.5 }} onClick={() => void download()}>
          Download the original document
        </Button>
      )}
      <DeliveryError error={error} onClose={() => setError(null)} />
    </Box>
  );
}
