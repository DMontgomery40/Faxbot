// Faxes → Received: import a PDF from another system (POST /imports). It lands in Received
// like a received fax, in the mailbox for its To number, and reaches the work queue.
import { useState } from 'react';
import { Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, TextField, Typography } from '@mui/material';
import { UploadFile as UploadFileIcon } from '@mui/icons-material';
import AdminAPIClient, { plainRefusal } from '../api/client';
import type { ImportManifest } from '../api/types';

export const IMPORT_SENTENCE = 'The document appears in Received like a received fax, in the mailbox for its To number.';

function newReference(): string {
  return `console-${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}`;
}

export default function ImportDocument({ client, onImported }: { client: AdminAPIClient; onImported?: () => void }) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState('Faxbot console');
  const [reference, setReference] = useState(newReference);
  const [revision, setRevision] = useState('');
  const [receivedAt, setReceivedAt] = useState('');
  const [to, setTo] = useState('');
  const [from, setFrom] = useState('');
  const [pages, setPages] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const start = () => {
    setFile(null); setSource('Faxbot console'); setReference(newReference()); setRevision(''); setReceivedAt('');
    setTo(''); setFrom(''); setPages(''); setError(null); setDone(null); setOpen(true);
  };

  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    const manifest: ImportManifest = { source_system: source.trim(), operation_id: reference.trim() };
    if (revision.trim()) manifest.revision = revision.trim();
    if (receivedAt) manifest.source_received_at = new Date(receivedAt).toISOString();
    if (to.trim()) manifest.to_number = to.trim();
    if (from.trim()) manifest.from_number = from.trim();
    if (pages.trim()) manifest.pages = Number(pages);
    try {
      const result = await client.importDocument(file, manifest);
      setDone(result.status === 'duplicate'
        ? 'This document was already imported with this number or ID; nothing new was added.'
        : 'Imported. It is in Received now.');
      onImported?.();
    } catch (failure) {
      setError(plainRefusal(failure)
        ?? (failure instanceof Error && /409/.test(failure.message)
          ? 'A different document was already imported with this number or ID; the first one is kept.'
          : 'The document could not be imported. Try again.'));
    } finally {
      setBusy(false);
    }
  };

  const ready = Boolean(file && source.trim() && reference.trim()) && !busy && !done;
  return (
    <>
      <Button variant="outlined" startIcon={<UploadFileIcon />} onClick={start}>Import a document</Button>
      <Dialog open={open} onClose={() => setOpen(false)} maxWidth="sm" fullWidth aria-labelledby="import-title">
        <DialogTitle id="import-title">Import a document</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 2 }}>{IMPORT_SENTENCE}</Typography>
          <Stack spacing={2}>
            <Box>
              <Button component="label" variant="outlined" startIcon={<UploadFileIcon />} disabled={busy || !!done}>
                {file ? file.name : 'Choose a PDF'}
                <input hidden type="file" accept="application/pdf,.pdf" data-testid="import-file"
                  onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
              </Button>
            </Box>
            <TextField size="small" label="Where it came from" value={source} required onChange={(event) => setSource(event.target.value)}
              helperText="The system or scanner that sent it." />
            <TextField size="small" label="Its number or ID in that system" value={reference} required onChange={(event) => setReference(event.target.value)}
              helperText="Importing a document with the same number or ID again never adds a second copy." />
            <TextField size="small" label="Version (optional)" value={revision} onChange={(event) => setRevision(event.target.value)}
              helperText="A new version with the same number or ID is kept as a separate document." />
            <TextField size="small" label="To number (optional)" value={to} type="tel" onChange={(event) => setTo(event.target.value)}
              helperText="The fax number it was meant for; it goes to that number's mailbox." />
            <TextField size="small" label="From number (optional)" value={from} type="tel" onChange={(event) => setFrom(event.target.value)} />
            <TextField size="small" label="When it was received (optional)" type="datetime-local" value={receivedAt}
              onChange={(event) => setReceivedAt(event.target.value)} InputLabelProps={{ shrink: true }} />
            <TextField size="small" label="Pages (optional)" type="number" value={pages} onChange={(event) => setPages(event.target.value)}
              inputProps={{ min: 1, max: 10000 }} />
          </Stack>
          {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
          {done && <Alert severity="success" sx={{ mt: 2 }}>{done}</Alert>}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>{done ? 'Close' : 'Cancel'}</Button>
          {!done && <Button variant="contained" onClick={() => void submit()} disabled={!ready}>Import</Button>}
        </DialogActions>
      </Dialog>
    </>
  );
}
