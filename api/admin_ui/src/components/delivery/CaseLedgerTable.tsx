// Recipients → Case packets: the documents one recipient was sent for a case, where each stands
// (delivered, acknowledged, too old, not found by them), and what a person records about them.
import { useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField,
  Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { CaseDocuments } from '../../api/deliveryTypes';
import type { CaseRepair } from '../../api/caseTypes';
import type { AdminDestination } from '../../navigation';
import {
  STATE_COLOR, STATE_LABEL, documentState, missingSentence, pagesText, stateSentence, versionAndSource,
} from './caseText';

const UNCONFIRMED = "Faxbot could not confirm whether the full packet was sent. Check Sent before sending it again.";

type Action = 'confirm' | 'missing' | 'repair' | null;

export default function CaseLedgerTable({ client, caseId, held, canSend, canWrite, onChanged, onError, onNavigate }: {
  client: AdminAPIClient;
  caseId: string;
  held: CaseDocuments;
  canSend: boolean;
  canWrite: boolean;
  onChanged: () => Promise<void> | void;
  onError: (error: unknown) => void;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const [chosen, setChosen] = useState<string[]>([]);
  const [action, setAction] = useState<Action>(null);
  const [note, setNote] = useState('');
  const [faxId, setFaxId] = useState('');
  const [repair, setRepair] = useState<CaseRepair | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [days, setDays] = useState<string>(String(held.reuse_days ?? held.reuse_days_default ?? 90));
  const [noLimit, setNoLimit] = useState(held.reuse_days === 0);

  const documents = held.documents;
  const ids = documents.map((document) => document.id).filter((value): value is string => Boolean(value));
  const toggle = (value: string) => setChosen((current) => (current.includes(value)
    ? current.filter((item) => item !== value) : [...current, value]));
  const close = () => { setAction(null); setNote(''); setFaxId(''); setRepair(null); };

  const run = async (step: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await step();
      setNotice(done);
      setChosen([]);
      close();
      await onChanged();
    } catch (failure) {
      onError(failure);
    } finally {
      setBusy(false);
    }
  };

  const confirm = () => run(() => client.acceptCaseDocuments(caseId, {
    to: held.to, documents: chosen, note: note.trim() || undefined, received_fax_id: faxId.trim() || undefined }),
  'Recorded. Later packets list these documents instead of sending them again, while the acknowledgement is trusted.');
  const missing = () => run(() => client.invalidateCaseDocuments(caseId, {
    to: held.to, documents: chosen, note: note.trim() || undefined }), 'Recorded. The next packet sends these documents in full.');

  const previewRepair = async () => {
    setBusy(true);
    try {
      setRepair(await client.repairCasePacket(caseId, { to: held.to, reason: note.trim(), preview: true }));
    } catch (failure) {
      onError(failure);
    } finally {
      setBusy(false);
    }
  };

  // Never retried here: a lost answer may mean the fax went.
  const sendRepair = async () => {
    setBusy(true);
    try {
      const result = await client.repairCasePacket(caseId, { to: held.to, reason: note.trim(), preview: false });
      setNotice(`The full packet is queued as a new fax: ${pagesText(result.pages)}.`);
      setChosen([]);
      close();
      await onChanged();
    } catch (failure) {
      onError(failure instanceof TypeError ? new Error(UNCONFIRMED) : failure);
    } finally {
      setBusy(false);
    }
  };

  const saveDays = () => run(
    () => client.setCaseReuseDays(held.to, noLimit ? 0 : Number(days), held.recipient_version ?? 0),
    'Saved. Acknowledgements older than this are not trusted, and those documents are sent in full again.');

  const reuseSentence = held.reuse_days === 0
    ? "This recipient's acknowledgements are trusted with no time limit."
    : `This recipient's acknowledgements are trusted for ${held.reuse_days ?? 90} days${held.reuse_days_set ? '' : " (Faxbot's default)"}.`;

  return (
    <Box>
      {notice && <Alert severity="success" sx={{ mb: 2, borderRadius: 2 }} onClose={() => setNotice(null)}>{notice}</Alert>}
      {documents.length === 0 ? (
        <Typography variant="body2" color="text.secondary">Nothing has been sent for this case to this recipient yet.</Typography>
      ) : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table size="small" aria-label="Documents already sent">
            <TableHead>
              <TableRow>
                {canSend && <TableCell padding="checkbox" />}
                <TableCell>Document</TableCell>
                <TableCell>Purpose</TableCell>
                <TableCell>Pages</TableCell>
                <TableCell>Where it stands</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {documents.map((document, index) => {
                const state = documentState(document);
                const detail = versionAndSource(document);
                const sentence = stateSentence(document, held.reuse_days);
                return (
                  <TableRow key={document.id ?? `${document.reference}-${index}`}>
                    {canSend && (
                      <TableCell padding="checkbox">
                        {document.id && (
                          <Checkbox size="small" checked={chosen.includes(document.id)} onChange={() => toggle(document.id as string)}
                            inputProps={{ 'aria-label': `Choose ${document.title}` }} />
                        )}
                      </TableCell>
                    )}
                    <TableCell>
                      {document.title}
                      {detail && <Typography variant="caption" color="text.secondary" display="block">{detail}</Typography>}
                    </TableCell>
                    <TableCell>{document.purpose || '-'}</TableCell>
                    <TableCell>{document.pages}</TableCell>
                    <TableCell>
                      <Chip size="small" variant="outlined" label={STATE_LABEL[state]} color={STATE_COLOR[state]} />
                      {sentence && <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>{sentence}</Typography>}
                      {document.fax_id && onNavigate && (
                        <Button size="small" sx={{ ml: -0.5 }} onClick={() => onNavigate('faxes/sent')}>See in Sent</Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      {canSend && documents.length > 0 && (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 2 }} data-testid="case-actions">
          <Button variant="outlined" disabled={chosen.length === 0 || busy} onClick={() => setAction('confirm')}>
            Recipient confirmed
          </Button>
          <Button variant="outlined" disabled={chosen.length === 0 || busy} onClick={() => setAction('missing')}>
            Recipient couldn't find it
          </Button>
          <Button variant="outlined" disabled={busy} onClick={() => setAction('repair')}>
            Repair: send the full packet
          </Button>
          {ids.length > 0 && (
            <Button size="small" disabled={busy} onClick={() => setChosen(chosen.length === ids.length ? [] : ids)}>
              {chosen.length === ids.length ? 'Clear the choice' : 'Choose all'}
            </Button>
          )}
        </Stack>
      )}

      <Box sx={{ mt: 2 }} data-testid="case-reuse">
        <Typography variant="body2" color="text.secondary">{reuseSentence}</Typography>
        {canWrite && (
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'center' }} sx={{ mt: 1 }}>
            <TextField size="small" type="number" label="Trust acknowledgements for (days)" value={days} disabled={noLimit}
              onChange={(event) => setDays(event.target.value)} inputProps={{ min: 1, max: 3650 }} sx={{ width: 260 }} />
            <FormControlLabel control={<Checkbox checked={noLimit} onChange={(event) => setNoLimit(event.target.checked)} />}
              label="No time limit" />
            <Button variant="outlined" size="small" disabled={busy || (!noLimit && !(Number(days) >= 1 && Number(days) <= 3650))}
              onClick={() => void saveDays()}>Save</Button>
          </Stack>
        )}
      </Box>

      <Dialog open={action === 'confirm' || action === 'missing'} onClose={close} fullWidth maxWidth="sm">
        <DialogTitle>{action === 'confirm' ? 'The recipient confirmed they have these documents' : "The recipient couldn't find these documents"}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {action === 'confirm'
              ? 'Later packets list these documents on the index page instead of sending them again, when the recipient accepts that.'
              : 'Faxbot stops listing these documents and sends them in full in the next packet. Nothing is sent now.'}
          </Typography>
          <TextField fullWidth multiline minRows={2} label="Note" value={note} onChange={(event) => setNote(event.target.value)}
            required={action === 'confirm' && !faxId.trim()} inputProps={{ maxLength: 500 }}
            helperText={action === 'confirm' ? 'Who confirmed it, and how. For example: their intake desk, by phone.' : 'What the recipient said.'} />
          {action === 'confirm' && (
            <TextField fullWidth sx={{ mt: 2 }} label="Fax ID of their acknowledgement (optional)" value={faxId}
              onChange={(event) => setFaxId(event.target.value)}
              helperText="If they faxed a confirmation, copy its ID from Received." />
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={close}>Cancel</Button>
          <Button variant="contained" disabled={busy || (action === 'confirm' && note.trim().length < 3 && !faxId.trim())}
            onClick={() => void (action === 'confirm' ? confirm() : missing())}>
            Record
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={action === 'repair'} onClose={close} fullWidth maxWidth="sm">
        <DialogTitle>Send the full packet again</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Every document of this case goes to {held.to} again as a new fax. Faxbot never does this by itself.
          </Typography>
          <TextField fullWidth multiline minRows={2} label="Why the recipient needs every document again" value={note}
            onChange={(event) => { setNote(event.target.value); }} inputProps={{ maxLength: 500 }} />
          {repair && (
            <Alert severity="info" sx={{ mt: 2, borderRadius: 2 }} data-testid="case-repair-plan">
              <Typography variant="body2" fontWeight={600}>{pagesText(repair.pages)} will be sent.</Typography>
              {repair.documents.map((document, index) => (
                <Typography key={`${document.title}-${index}`} variant="body2">{document.title} · {pagesText(document.pages)}</Typography>
              ))}
              {repair.missing.map((entry, index) => (
                <Typography key={`${entry.title}-${index}`} variant="body2">{missingSentence(entry)}</Typography>
              ))}
              {repair.packets_in_flight > 0 && (
                <Typography variant="body2">An earlier packet for this case has not finished sending.</Typography>
              )}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={close}>Cancel</Button>
          <Button onClick={() => void previewRepair()} disabled={busy}
            startIcon={busy && !repair ? <CircularProgress size={16} /> : undefined}>Preview</Button>
          <Button variant="contained" onClick={() => void sendRepair()} disabled={busy || !repair || note.trim().length < 3}>
            Send the full packet
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
