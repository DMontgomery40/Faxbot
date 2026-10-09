// Recipients → Case packets → Build from a checklist: the case's kept documents, the recipient's checklist,
// Faxbot's picks with a reason each, the missing items, and sending only what a person checked.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel,
  IconButton, MenuItem, Paper, Stack, Switch, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField,
  Tooltip, Typography,
} from '@mui/material';
import DeleteIcon from '@mui/icons-material/Delete';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import AdminAPIClient from '../../api/client';
import type {
  CaseChecklists, CaseOriginal, CaseOriginalDraft, ChecklistBuild, ChecklistItem,
} from '../../api/caseTypes';
import { formatLocalDate, localDay } from '../../api/time';
import { WHY_LABEL, pagesText, retentionSentence, versionAndSource } from './caseText';

const UNCONFIRMED = "Faxbot could not confirm whether the packet was sent. Check Sent before sending it again.";
const BLANK_ITEM: ChecklistItem = { type: '', required: true, within_days: null, version: null };

function today(): string {
  return localDay(new Date().toISOString());
}

interface Choice { original_id: string; item: number | null; title: string; reason: string; suggested: boolean }

export default function CaseChecklistBuilder({ client, caseId, to, canSend, canWrite, onSent, onError }: {
  client: AdminAPIClient;
  caseId: string;
  to: string;
  canSend: boolean;
  canWrite: boolean;
  onSent: () => Promise<void> | void;
  onError: (error: unknown) => void;
}) {
  const [originals, setOriginals] = useState<CaseOriginal[] | null>(null);
  const [retentionDays, setRetentionDays] = useState<number | undefined>(undefined);
  const [lists, setLists] = useState<CaseChecklists | null>(null);
  const [drafts, setDrafts] = useState<CaseOriginalDraft[]>([]);
  const [checklistId, setChecklistId] = useState('');
  const [asOf, setAsOf] = useState(today());
  const [purpose, setPurpose] = useState('');
  const [built, setBuilt] = useState<ChecklistBuild | null>(null);
  const [choices, setChoices] = useState<Choice[]>([]);
  const [checked, setChecked] = useState<string[]>([]);
  const [allowMissing, setAllowMissing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ name: string; items: ChecklistItem[] } | null>(null);

  const load = useCallback(async () => {
    try {
      const [kept, available] = await Promise.all([client.listCaseOriginals(caseId), client.listCaseChecklists()]);
      setOriginals(kept.originals);
      setRetentionDays(kept.retention_days);
      setLists(available);
      setChecklistId((current) => current || available.checklists[0]?.id || '');
    } catch (failure) {
      onError(failure);
    }
  }, [client, caseId, onError]);

  useEffect(() => { void load(); }, [load]);

  const step = async (name: string, action: () => Promise<void>) => {
    setBusy(name);
    try {
      await action();
    } catch (failure) {
      onError(failure);
    } finally {
      setBusy(null);
    }
  };

  const keep = () => step('keep', async () => {
    const result = await client.addCaseOriginals(caseId, drafts);
    setOriginals(result.originals);
    setDrafts([]);
    setBuilt(null);
    setNotice('Kept in the case, unchanged.');
  });

  const choose = (result: ChecklistBuild) => {
    const picked: Choice[] = result.selected.map((pick) => ({ ...pick, suggested: false }));
    const suggested: Choice[] = result.suggestions.map((entry) => ({ ...entry, suggested: true }));
    setChoices([...picked, ...suggested]);
    setChecked(picked.map((pick) => `${pick.original_id}:${pick.item}`));
    setAllowMissing(false);
  };

  const preview = () => step('preview', async () => {
    const result = await client.buildChecklistPacket(caseId, { to, checklist_id: checklistId, as_of: asOf, purpose, preview: true });
    setBuilt(result);
    choose(result);
  });

  const selection = () => choices.filter((choice) => checked.includes(`${choice.original_id}:${choice.item}`))
    .map((choice) => ({ original_id: choice.original_id, item: choice.item }));

  // Re-preview with the checked documents, so pages and missing items match what will be sent.
  const recheck = () => step('preview', async () => {
    if (!built) return;
    const result = await client.buildChecklistPacket(caseId, { to, checklist_id: built.checklist.id, as_of: built.as_of,
      purpose, preview: true, selection: selection() });
    setBuilt({ ...result, suggestions: built.suggestions });
  });

  // Never retried here: a lost answer may mean the packet went.
  const send = () => step('send', async () => {
    if (!built) return;
    try {
      const result = await client.buildChecklistPacket(caseId, { to, checklist_id: built.checklist.id, as_of: built.as_of,
        purpose, preview: false, selection: selection(), allow_missing: allowMissing });
      setNotice(`Queued to send: ${pagesText(result.packet?.pages ?? 0)}.`);
      setBuilt(null);
      setChoices([]);
      await onSent();
    } catch (failure) {
      throw failure instanceof TypeError ? new Error(UNCONFIRMED) : failure;
    }
  });

  const saveChecklist = () => step('checklist', async () => {
    if (!editing) return;
    const saved = await client.addCaseChecklist({ name: editing.name, items: editing.items, to });
    setEditing(null);
    await load();
    setChecklistId(saved.id);
    setNotice(saved.version > 1
      ? `Saved as version ${saved.version}. Earlier versions stay as they were.` : 'Checklist saved.');
  });

  const toggleSuggestions = (on: boolean) => step('suggestions', async () => {
    const current = await client.getSettings();
    const revision = current._meta?.desired_revision_id;
    if (!revision) throw new Error('Settings could not be loaded.');
    await client.updateSettings({ expected_revision_id: revision, case_suggestions_enabled: on });
    setLists((value) => (value ? { ...value, suggestions: on } : value));
  });

  const key = (choice: Choice) => `${choice.original_id}:${choice.item}`;
  const items = built?.items ?? [];
  const uncovered = items.map((item, index) => ({ item, index }))
    .filter(({ item, index }) => item.required && !choices.some((choice) => choice.item === index && checked.includes(key(choice))));

  return (
    <Box data-testid="case-checklist">
      {notice && <Alert severity="success" sx={{ mb: 2, borderRadius: 2 }} onClose={() => setNotice(null)}>{notice}</Alert>}

      <Typography variant="subtitle1" component="h3" sx={{ mb: 1 }}>Documents kept in this case</Typography>
      {originals && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }} data-testid="case-retention">
          {retentionSentence(retentionDays)}
        </Typography>
      )}
      {originals && originals.length === 0 && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          No documents are kept for this case yet. Documents you send are kept, and you can add more here.
        </Typography>
      )}
      {originals && originals.length > 0 && (
        <TableContainer component={Paper} sx={{ borderRadius: 2, mb: 2 }}>
          <Table size="small" aria-label="Documents kept in this case">
            <TableHead>
              <TableRow>
                <TableCell>Document</TableCell>
                <TableCell>Type</TableCell>
                <TableCell>Dated</TableCell>
                <TableCell>Pages</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {originals.map((row) => (
                <TableRow key={row.id}>
                  <TableCell>
                    {row.title}
                    {versionAndSource(row) && <Typography variant="caption" color="text.secondary" display="block">{versionAndSource(row)}</Typography>}
                  </TableCell>
                  <TableCell>{row.document_type || '-'}</TableCell>
                  <TableCell>{row.document_date ? formatLocalDate(row.document_date.slice(0, 10)) : '-'}</TableCell>
                  <TableCell>{row.pages}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {canSend && (
        <Box sx={{ mb: 3 }}>
          <Button component="label" variant="outlined" size="small" startIcon={<UploadFileIcon />} disabled={busy !== null}>
            Add documents to the case
            <input hidden type="file" accept="application/pdf,.pdf" multiple data-testid="case-original-files"
              onChange={(event) => {
                const files = Array.from(event.target.files ?? []);
                setDrafts((current) => [...current, ...files.map((file) => ({ file, title: file.name.replace(/\.pdf$/i, ''), type: '', date: '', version: '', source: '' }))]);
                event.target.value = '';
              }} />
          </Button>
          {drafts.length > 0 && (
            <Stack spacing={1} sx={{ mt: 2 }}>
              {drafts.map((draft, index) => {
                const change = (field: keyof CaseOriginalDraft, value: string) => setDrafts((current) =>
                  current.map((item, at) => (at === index ? { ...item, [field]: value } : item)));
                return (
                  <Stack key={`${draft.file.name}-${index}`} direction={{ xs: 'column', md: 'row' }} spacing={1} alignItems={{ md: 'center' }}>
                    <TextField size="small" label="Title" value={draft.title} onChange={(event) => change('title', event.target.value)} />
                    <TextField size="small" label="Document type" value={draft.type} placeholder="Discharge summary"
                      onChange={(event) => change('type', event.target.value)} />
                    <TextField size="small" label="Date on the document" type="date" value={draft.date} InputLabelProps={{ shrink: true }}
                      onChange={(event) => change('date', event.target.value)} />
                    <TextField size="small" label="Version" value={draft.version} onChange={(event) => change('version', event.target.value)} />
                    <TextField size="small" label="Source" value={draft.source} onChange={(event) => change('source', event.target.value)} />
                    <Tooltip title="Remove">
                      <IconButton aria-label={`Remove ${draft.title || draft.file.name}`}
                        onClick={() => setDrafts((current) => current.filter((_, at) => at !== index))}><DeleteIcon /></IconButton>
                    </Tooltip>
                  </Stack>
                );
              })}
              <Box><Button variant="contained" size="small" onClick={() => void keep()} disabled={busy !== null}>Keep these documents</Button></Box>
            </Stack>
          )}
        </Box>
      )}

      <Typography variant="subtitle1" component="h3" sx={{ mb: 1 }}>The recipient's checklist</Typography>
      {lists && lists.checklists.length === 0 && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>No checklists yet.</Typography>
      )}
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'center' }} sx={{ mb: 2 }}>
        {lists && lists.checklists.length > 0 && (
          <TextField select size="small" label="Checklist" value={checklistId} sx={{ minWidth: 260 }}
            onChange={(event) => { setChecklistId(event.target.value); setBuilt(null); }}>
            {lists.checklists.map((item) => (
              <MenuItem key={item.id} value={item.id}>{item.name} (version {item.version})</MenuItem>
            ))}
          </TextField>
        )}
        {canWrite && lists && (
          <Button size="small" variant="outlined" onClick={() => setEditing({ name: '', items: [{ ...BLANK_ITEM }] })}>New checklist</Button>
        )}
      </Stack>
      {canWrite && lists && (
        <FormControlLabel sx={{ mb: 2 }} control={<Switch checked={lists.suggestions} disabled={busy !== null}
          onChange={(event) => void toggleSuggestions(event.target.checked)} />}
          label="Suggest documents that may match a missing item. Nothing suggested is sent unless you check it." />
      )}

      {canSend && checklistId && (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'center' }} sx={{ mb: 2 }}>
          <TextField size="small" type="date" label="Count dates back from" value={asOf} InputLabelProps={{ shrink: true }}
            onChange={(event) => { setAsOf(event.target.value); setBuilt(null); }} />
          <TextField size="small" label="Purpose of the packet" value={purpose} onChange={(event) => setPurpose(event.target.value)} />
          <Button variant="outlined" onClick={() => void preview()} disabled={busy !== null}
            startIcon={busy === 'preview' ? <CircularProgress size={16} /> : undefined}>Build the packet</Button>
        </Stack>
      )}

      {built && (
        <Box data-testid="checklist-preview">
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            {built.checklist.name}, version {built.checklist.version}, with dates counted back from {formatLocalDate(built.as_of)}.
          </Typography>
          {choices.length === 0 && (
            <Typography variant="body2" sx={{ mb: 1 }}>No kept document matches an item on this checklist.</Typography>
          )}
          {choices.map((choice) => (
            <Box key={key(choice)} display="flex" alignItems="flex-start" gap={1}>
              <Checkbox size="small" checked={checked.includes(key(choice))} inputProps={{ 'aria-label': `Send ${choice.title}` }}
                onChange={() => setChecked((current) => (current.includes(key(choice)) ? current.filter((value) => value !== key(choice)) : [...current, key(choice)]))} />
              <Box sx={{ pt: 1 }}>
                <Typography variant="body2">
                  {choice.item !== null && items[choice.item] ? `${items[choice.item].type}: ` : ''}{choice.title}
                  {choice.suggested ? ' (suggested)' : ''}
                </Typography>
                <Typography variant="caption" color="text.secondary">{choice.reason}</Typography>
              </Box>
            </Box>
          ))}
          {built.missing.length > 0 && (
            <Box sx={{ mt: 1 }}>
              {built.missing.map((entry) => (
                <Typography key={entry.item} variant="body2" color={entry.required ? 'error' : 'text.secondary'}>
                  {entry.required ? 'Missing' : 'Missing (optional)'}: {entry.type}. {entry.reason}
                </Typography>
              ))}
            </Box>
          )}
          {built.packet && (
            <Alert severity="info" sx={{ mt: 2, borderRadius: 2 }}>
              {pagesText(built.packet.pages)} will be sent{built.packet.pages_saved > 0 ? `; ${pagesText(built.packet.pages_saved)} left out` : ''}.
              {built.packet.documents.filter((document) => document.status === 'referenced').map((document) => (
                <Typography key={document.title} variant="body2">{document.title}: {WHY_LABEL.accepted}, listed on the index</Typography>
              ))}
            </Alert>
          )}
          {uncovered.length > 0 && (
            <FormControlLabel sx={{ mt: 1 }} control={<Checkbox checked={allowMissing} onChange={(event) => setAllowMissing(event.target.checked)} />}
              label={`Send without ${uncovered.map(({ item }) => item.type).join(', ')}; the recipient agreed`} />
          )}
          <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
            <Button variant="outlined" onClick={() => void recheck()} disabled={busy !== null}>Update the preview</Button>
            <Button variant="contained" onClick={() => void send()}
              disabled={busy !== null || checked.length === 0 || (uncovered.length > 0 && !allowMissing)}
              startIcon={busy === 'send' ? <CircularProgress size={16} color="inherit" /> : undefined}>
              Send the checked documents
            </Button>
          </Stack>
        </Box>
      )}

      <Dialog open={editing !== null} onClose={() => setEditing(null)} fullWidth maxWidth="md">
        <DialogTitle>New checklist</DialogTitle>
        {editing && (
          <DialogContent>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              List the document types the recipient asks for. Saving a name already in use makes a new version; earlier versions never change.
            </Typography>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ mb: 2 }}>
              <TextField size="small" fullWidth label="Name" value={editing.name} placeholder="Mesa payer: prior authorization"
                onChange={(event) => setEditing({ ...editing, name: event.target.value })} />
              {lists && (
                <Button size="small" onClick={() => setEditing({ name: editing.name || lists.example.name, items: lists.example.items.map((item) => ({ ...item })) })}>
                  Start from the example
                </Button>
              )}
            </Stack>
            {editing.items.map((item, index) => {
              const change = (patch: Partial<ChecklistItem>) => setEditing({ ...editing, items: editing.items.map((entry, at) => (at === index ? { ...entry, ...patch } : entry)) });
              return (
                <Stack key={index} direction={{ xs: 'column', md: 'row' }} spacing={1} alignItems={{ md: 'center' }} sx={{ mb: 1 }}>
                  <TextField size="small" label="Document type" value={item.type} onChange={(event) => change({ type: event.target.value })} />
                  <TextField size="small" type="number" label="Dated within (days)" value={item.within_days ?? ''}
                    onChange={(event) => change({ within_days: event.target.value ? Number(event.target.value) : null })} />
                  <TextField size="small" label="Version" value={item.version ?? ''} onChange={(event) => change({ version: event.target.value || null })} />
                  <FormControlLabel control={<Checkbox checked={item.required} onChange={(event) => change({ required: event.target.checked })} />} label="Required" />
                  <Tooltip title="Remove">
                    <IconButton aria-label={`Remove item ${index + 1}`} disabled={editing.items.length === 1}
                      onClick={() => setEditing({ ...editing, items: editing.items.filter((_, at) => at !== index) })}><DeleteIcon /></IconButton>
                  </Tooltip>
                </Stack>
              );
            })}
            <Button size="small" onClick={() => setEditing({ ...editing, items: [...editing.items, { ...BLANK_ITEM }] })}>Add an item</Button>
          </DialogContent>
        )}
        <DialogActions>
          <Button onClick={() => setEditing(null)}>Cancel</Button>
          <Button variant="contained" onClick={() => void saveChecklist()}
            disabled={busy !== null || !editing?.name.trim() || !editing?.items.every((item) => item.type.trim())}>Save</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
