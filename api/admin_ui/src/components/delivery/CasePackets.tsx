// Recipients → Case packets: for one case and one recipient, which documents they
// acknowledged, and sending a packet that leaves those out (a one-page index lists
// them instead, when the recipient accepts that). Delivered is not acknowledged.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, Paper, Stack, Table, TableBody, TableCell, TableContainer,
  TableHead, TableRow, TextField, Tooltip, Typography,
} from '@mui/material';
import DeleteIcon from '@mui/icons-material/Delete';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import AdminAPIClient from '../../api/client';
import type { CaseDocuments, CasePacket, CaseSummary } from '../../api/deliveryTypes';
import { formatServerTime } from '../../api/time';
import CaseChecklistBuilder from './CaseChecklistBuilder';
import CaseLedgerTable from './CaseLedgerTable';
import { WHY_LABEL, pagesText } from './caseText';
import type { AdminDestination } from '../../navigation';
import { ScreenHeader } from '../access/AccessViews';
import { DeliveryError } from './shared';
import { DestinationDialog } from './Destinations';
import { numberPlaceholder, useNumberFormat } from '../common/numbers';

interface Draft { file: File; title: string; version: string; source: string }

const UNCONFIRMED = "Faxbot could not confirm whether the packet was sent. Check Sent before sending it again.";

export default function CasePackets({ client, canSend, canWrite, onNavigate }: {
  client: AdminAPIClient;
  // May this person send faxes (the packet is a fax)?
  canSend: boolean;
  // May this person change a recipient's details (whether it accepts an index)?
  canWrite: boolean;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const numberFormat = useNumberFormat(client);
  const [caseId, setCaseId] = useState('');
  const [to, setTo] = useState('');
  const [held, setHeld] = useState<CaseDocuments | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [plan, setPlan] = useState<CasePacket | null>(null);
  const [sent, setSent] = useState<CasePacket | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<'look' | 'preview' | 'send' | null>(null);
  const [details, setDetails] = useState<string | null>(null);
  // The newest cases this installation sent packets for; null until loaded or when they could not be read.
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [purpose, setPurpose] = useState('');
  const [checklistOpen, setChecklistOpen] = useState(false);

  const ready = Boolean(caseId.trim() && to.trim());

  const loadCases = useCallback(async () => {
    try {
      setCases((await client.listCases()).cases);
    } catch {
      setCases(null);
    }
  }, [client]);

  useEffect(() => { void loadCases(); }, [loadCases]);

  const lookUp = async (reference = caseId.trim(), recipient = to.trim()) => {
    setBusy('look');
    setError(null);
    setSent(null);
    try {
      setHeld(await client.getCaseDocuments(reference, recipient));
    } catch (failure) {
      setHeld(null);
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  const addFiles = (files: FileList | null) => {
    if (!files) return;
    setPlan(null);
    setSent(null);
    setDrafts((current) => [...current, ...Array.from(files).map((file) => ({ file, title: file.name.replace(/\.pdf$/i, ''), version: '', source: '' }))]);
  };

  const preview = async () => {
    setBusy('preview');
    setError(null);
    setSent(null);
    try {
      setPlan(await client.sendCasePacket(caseId.trim(), to.trim(), drafts, true, purpose.trim()));
    } catch (failure) {
      setPlan(null);
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  // Sending is never retried here: a lost answer may mean the packet went.
  const send = async () => {
    setBusy('send');
    setError(null);
    try {
      const result = await client.sendCasePacket(caseId.trim(), to.trim(), drafts, false, purpose.trim());
      setSent(result);
      setPlan(null);
      setDrafts([]);
      setHeld(await client.getCaseDocuments(caseId.trim(), to.trim()).catch(() => held));
      void loadCases();
    } catch (failure) {
      setError(failure instanceof TypeError ? new Error(UNCONFIRMED) : failure);
    } finally {
      setBusy(null);
    }
  };

  const changed = () => { setPlan(null); setSent(null); };
  const reload = async () => {
    try {
      setHeld(await client.getCaseDocuments(caseId.trim(), to.trim()));
    } catch (failure) {
      setError(failure);
    }
    void loadCases();
  };

  return (
    <Box>
      <ScreenHeader title="Case packets"
        subtitle="When you fax documents for a case, Faxbot leaves out the ones the recipient acknowledged and lists them on a one-page index instead." />
      <DeliveryError error={error} onClose={() => setError(null)} />

      <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, mb: 3 }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'center' }}>
          <TextField size="small" label="Case reference" value={caseId} sx={{ minWidth: 220 }}
            onChange={(event) => { setCaseId(event.target.value); setHeld(null); changed(); }}
            helperText="Letters, numbers, dots, dashes or colons." />
          <TextField size="small" label="Recipient fax number" value={to} type="tel" sx={{ minWidth: 220 }}
            placeholder={numberPlaceholder(numberFormat)}
            onChange={(event) => { setTo(event.target.value); setHeld(null); changed(); }} helperText=" " />
          <Button variant="outlined" onClick={() => void lookUp()} disabled={!ready || busy !== null} sx={{ borderRadius: 2, mb: { sm: 3 } }}>
            Look up
          </Button>
        </Stack>
      </Paper>

      {held && (
        <Box component="section" sx={{ mb: 4 }} data-testid="case-documents">
          <Typography variant="h6" component="h2">Already sent to {held.to} for this case</Typography>
          <Box display="flex" alignItems="center" gap={1} flexWrap="wrap" sx={{ mb: 2 }}>
            <Typography variant="body2" color="text.secondary">
              {held.accepts_references
                ? 'This recipient accepts a one-page list instead of documents it already has.'
                : 'This recipient wants every document in full, even ones it already has.'}
            </Typography>
            <Button size="small" onClick={() => setDetails(held.to)}>Recipient details</Button>
          </Box>
          <CaseLedgerTable key={`${held.case_id} ${held.to}`} client={client} caseId={held.case_id} held={held}
            canSend={canSend} canWrite={canWrite} onChanged={reload} onError={setError} onNavigate={onNavigate} />
        </Box>
      )}

      {held && canSend && (
        <Box component="section" data-testid="case-send">
          <Typography variant="h6" component="h2">Fax the documents</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Add the case's documents as PDF files. Preview shows what Faxbot will leave out, and why each document is sent, before anything is sent.
          </Typography>
          <Button component="label" variant="outlined" startIcon={<UploadFileIcon />} disabled={busy !== null} sx={{ borderRadius: 2, mb: 2 }}>
            Add PDF documents
            <input hidden type="file" accept="application/pdf,.pdf" multiple data-testid="case-files"
              onChange={(event) => { addFiles(event.target.files); event.target.value = ''; }} />
          </Button>
          {drafts.length > 0 && (
            <Stack spacing={1} sx={{ mb: 2 }}>
              {drafts.map((draft, index) => (
                <Box key={`${draft.file.name}-${index}`} display="flex" gap={1} alignItems="center" flexWrap={{ xs: 'wrap', md: 'nowrap' }}>
                  <TextField size="small" fullWidth label={`Title of document ${index + 1}`} value={draft.title}
                    onChange={(event) => { changed(); setDrafts((current) => current.map((item, at) => (at === index ? { ...item, title: event.target.value } : item))); }} />
                  <TextField size="small" label="Version" value={draft.version} sx={{ minWidth: 120 }}
                    onChange={(event) => { changed(); setDrafts((current) => current.map((item, at) => (at === index ? { ...item, version: event.target.value } : item))); }} />
                  <TextField size="small" label="Source" value={draft.source} sx={{ minWidth: 160 }}
                    onChange={(event) => { changed(); setDrafts((current) => current.map((item, at) => (at === index ? { ...item, source: event.target.value } : item))); }} />
                  <Tooltip title="Remove">
                    <IconButton aria-label={`Remove ${draft.title || draft.file.name}`}
                      onClick={() => { changed(); setDrafts((current) => current.filter((_, at) => at !== index)); }}>
                      <DeleteIcon />
                    </IconButton>
                  </Tooltip>
                </Box>
              ))}
            </Stack>
          )}
          {drafts.length > 0 && (
            <TextField size="small" label="Purpose of the packet" value={purpose} sx={{ mb: 2, minWidth: 320 }}
              helperText="The same document sent for another purpose goes in full."
              onChange={(event) => { changed(); setPurpose(event.target.value); }} />
          )}
          <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
            <Button variant="outlined" onClick={() => void preview()} disabled={drafts.length === 0 || busy !== null}
              startIcon={busy === 'preview' ? <CircularProgress size={16} /> : undefined}>
              Preview
            </Button>
            <Button variant="contained" onClick={() => void send()} disabled={!plan || busy !== null}
              startIcon={busy === 'send' ? <CircularProgress size={16} color="inherit" /> : undefined}>
              Send the documents
            </Button>
          </Stack>
          {plan && (
            <Alert severity="info" sx={{ mb: 2, borderRadius: 2 }} data-testid="case-plan">
              <Typography variant="body2" fontWeight={600}>
                {pagesText(plan.pages)} will be sent{plan.pages_saved > 0 ? `; ${pagesText(plan.pages_saved)} left out` : ''}.
              </Typography>
              {plan.documents.map((document, index) => (
                <Box key={`${document.title}-${index}`} display="flex" gap={1} alignItems="center" sx={{ mt: 0.5 }}>
                  <Chip size="small" label={document.status === 'included' ? 'Sent' : 'Listed on the index'}
                    color={document.status === 'included' ? 'primary' : 'default'} variant="outlined" />
                  <Typography variant="body2">
                    {document.title} · {pagesText(document.pages)}
                    {document.status === 'included' && document.why ? ` · ${WHY_LABEL[document.why]}` : ''}
                  </Typography>
                </Box>
              ))}
            </Alert>
          )}
          {sent && (
            <Alert severity="success" sx={{ borderRadius: 2 }} data-testid="case-sent"
              action={onNavigate && <Button color="inherit" size="small" onClick={() => onNavigate('faxes/sent')}>See it in Sent</Button>}>
              Queued to send: {pagesText(sent.pages)}{sent.pages_saved > 0
                ? `, and ${pagesText(sent.pages_saved)} left out because the recipient already has them` : ''}.
            </Alert>
          )}
        </Box>
      )}

      {held && (canSend || canWrite) && (
        <Box component="section" sx={{ mt: 4 }}>
          <Typography variant="h6" component="h2">Build a packet from a checklist</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Faxbot picks the documents the recipient's checklist asks for from the ones kept in this case, says why for each, and lists what is missing.
          </Typography>
          {checklistOpen ? (
            <CaseChecklistBuilder client={client} caseId={held.case_id} to={held.to} canSend={canSend} canWrite={canWrite}
              onSent={reload} onError={setError} />
          ) : (
            <Button variant="outlined" onClick={() => setChecklistOpen(true)}>Open the checklist builder</Button>
          )}
        </Box>
      )}

      {cases && cases.length === 0 && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 4 }} data-testid="no-cases">No case packets have been sent yet.</Typography>
      )}

      {cases && cases.length > 0 && (
        <Box component="section" sx={{ mt: 4 }} data-testid="recent-cases">
          <Typography variant="h6" component="h2" sx={{ mb: 1 }}>Recent cases</Typography>
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table size="small" aria-label="Recent cases">
              <TableHead>
                <TableRow>
                  <TableCell>Case</TableCell>
                  <TableCell>Recipient</TableCell>
                  <TableCell>Documents</TableCell>
                  <TableCell>Pages</TableCell>
                  <TableCell>Last sent</TableCell>
                  <TableCell />
                </TableRow>
              </TableHead>
              <TableBody>
                {cases.map((item) => (
                  <TableRow key={`${item.case_id} ${item.to}`}>
                    <TableCell>{item.case_id}</TableCell>
                    <TableCell>{item.to}</TableCell>
                    <TableCell>{`${item.documents} sent, ${item.accepted} acknowledged`}</TableCell>
                    <TableCell>{item.pages}</TableCell>
                    <TableCell>{formatServerTime(item.last_sent_at)}</TableCell>
                    <TableCell>
                      <Button size="small" disabled={busy !== null}
                        aria-label={`Open case ${item.case_id} for ${item.to}`}
                        onClick={() => {
                          setCaseId(item.case_id);
                          setTo(item.to);
                          changed();
                          void lookUp(item.case_id, item.to);
                        }}>
                        Open
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Box>
      )}

      <DestinationDialog client={client} number={details} canWrite={canWrite} onClose={() => setDetails(null)}
        onSaved={() => { setDetails(null); void lookUp(); }} />
    </Box>
  );
}
