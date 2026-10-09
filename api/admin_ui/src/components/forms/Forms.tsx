// Faxes → Forms: registered forms, filling one in and sending it, and the forms you sent.
// A partner running Faxbot gets only the filled-in values and draws identical pages
// itself; anyone else gets the same pages as a fax.
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, MenuItem, Paper, Stack, Switch, Tab, Table, TableBody, TableCell, TableContainer, TableHead,
  TableRow, Tabs, TextField, Typography,
} from '@mui/material';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import AdminAPIClient, { AdminAPIError } from '../../api/client';
import type { DirectPartner } from '../../api/deliveryTypes';
import type {
  FormDelivery, FormField, FormValue, FormVersionDetail, PartnerForms, RegisteredForm,
} from '../../api/formsTypes';
import { formatServerTime } from '../../api/time';
import { ScreenHeader } from '../access/AccessViews';
import { DeliveryError, Notice, deliveryErrorMessage } from '../delivery/shared';
import { FormValuesTable } from './ReceivedFormLine';

export type FormsTab = 'forms' | 'send' | 'sent';

const UNCONFIRMED = 'Faxbot could not confirm whether the form was sent. Check the Sent forms tab before sending it again.';

// Field problems come back as plain sentences; show them as written.
function formsErrorMessage(error: unknown): string {
  if (error instanceof AdminAPIError && [400, 409, 413, 422].includes(error.status) && error.detail
      && /^[A-Z][^<>{}]{3,300}[.!?]$/.test(error.detail)) {
    return error.detail;
  }
  return deliveryErrorMessage(error);
}

function FormsError({ error, onClose }: { error: unknown; onClose: () => void }) {
  if (!error) return null;
  return <Alert severity="error" sx={{ mb: 3, borderRadius: 2 }} onClose={onClose}>{formsErrorMessage(error)}</Alert>;
}

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}

function digits(value: string): string {
  return value.replace(/\D/g, '');
}

// -- Forms and versions ------------------------------------------------------------------------

function ImportDialog({ client, form, onClose, onImported }: {
  client: AdminAPIClient;
  // A form to add a version to; null imports a new form.
  form: RegisteredForm | null;
  onClose: () => void;
  onImported: (message: string, versionId: string) => void;
}) {
  const [name, setName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [positions, setPositions] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ready = Boolean(file && (form || name.trim()));

  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await client.importForm(file, { name: name.trim(), formId: form?.id, positions });
      onImported(result.message, result.version.id);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>{form ? `New version of ${form.name}` : 'Import a form'}</DialogTitle>
      <DialogContent>
        <FormsError error={error} onClose={() => setError(null)} />
        <Stack spacing={2} sx={{ mt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            Choose a fillable PDF, and Faxbot reads its fields. For a PDF or SVG without fillable fields, also choose a
            field-position file that places each field.{form ? ' Earlier versions stay exactly as they are.' : ''}
          </Typography>
          {!form && <TextField label="Form name" value={name} onChange={(event) => setName(event.target.value)} required
            inputProps={{ maxLength: 200 }} />}
          <Button variant="outlined" component="label" startIcon={<UploadFileIcon />}>
            {file ? file.name : 'Choose the form (PDF or SVG)'}
            <input hidden type="file" accept=".pdf,.svg,application/pdf,image/svg+xml"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          </Button>
          <Button variant="text" component="label">
            {positions ? `Field positions: ${positions.name}` : 'Add a field-position file (optional)'}
            <input hidden type="file" accept=".json,application/json"
              onChange={(event) => setPositions(event.target.files?.[0] ?? null)} />
          </Button>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={!ready || busy} onClick={() => void submit()}>
          {busy ? 'Importing…' : 'Import'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function VersionDetail({ client, versionId }: { client: AdminAPIClient; versionId: string }) {
  const [detail, setDetail] = useState<FormVersionDetail | null>(null);
  const [page, setPage] = useState(1);
  const [outlined, setOutlined] = useState(true);
  const [picture, setPicture] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setPage(1);
    client.getFormVersion(versionId).then((found) => { if (!cancelled) setDetail(found); })
      .catch((failure) => { if (!cancelled) setError(failure); });
    return () => { cancelled = true; };
  }, [client, versionId]);

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    client.formPagePicture(versionId, page, outlined).then((blob) => {
      if (cancelled) return;
      url = URL.createObjectURL(blob);
      setPicture(url);
    }).catch(() => { if (!cancelled) setPicture(null); });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [client, versionId, page, outlined]);

  if (error) return <FormsError error={error} onClose={() => setError(null)} />;
  if (!detail) return <CircularProgress size={24} />;
  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {detail.source_text} {plural(detail.pages, 'page')}, {plural(detail.fields, 'field')}. Added {formatServerTime(detail.created_at)}.
      </Typography>
      <TableContainer component={Paper} variant="outlined">
        <Table size="small" aria-label="Fields">
          <TableHead>
            <TableRow>
              <TableCell>Field</TableCell><TableCell>Type</TableCell><TableCell>Page</TableCell>
              <TableCell>Required</TableCell><TableCell>Choices or format</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {detail.field_list.map((field) => (
              <TableRow key={field.name}>
                <TableCell>{field.label}</TableCell>
                <TableCell>{field.type_text}</TableCell>
                <TableCell>{field.page}</TableCell>
                <TableCell>{field.required ? 'Yes' : 'No'}</TableCell>
                <TableCell>{field.options?.join(', ') || field.format
                  || (field.decimals !== undefined ? plural(field.decimals, 'decimal place') : '')}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
      <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
        {detail.pages > 1 && (
          <TextField select size="small" label="Page" value={page} onChange={(event) => setPage(Number(event.target.value))}>
            {Array.from({ length: detail.pages }, (_, index) => <MenuItem key={index} value={index + 1}>{index + 1}</MenuItem>)}
          </TextField>
        )}
        <FormControlLabel control={<Switch checked={outlined} onChange={(event) => setOutlined(event.target.checked)} />}
          label="Outline the fields" />
        {detail.has_template && (
          <Button size="small" onClick={() => void client.downloadFormTemplate(versionId)
            .then((blob) => saveBlob(blob, `${detail.form_name} v${detail.number}${blob.type.includes('svg') ? '.svg' : '.pdf'}`))
            .catch(setError)}>Download the imported file</Button>
        )}
      </Stack>
      {picture && (
        <Box component="img" src={picture} alt={`Page ${page} of ${detail.form_name}, as it is faxed`}
          sx={{ maxWidth: '100%', border: '1px solid', borderColor: 'divider', borderRadius: 1, bgcolor: 'common.white' }} />
      )}
    </Stack>
  );
}

function FormsList({ client, forms, canWrite, onChanged, onSend }: {
  client: AdminAPIClient;
  forms: RegisteredForm[] | null;
  canWrite: boolean;
  onChanged: (message: string) => void;
  onSend: (form: RegisteredForm) => void;
}) {
  const [importing, setImporting] = useState<RegisteredForm | null | 'new'>(null);
  const [open, setOpen] = useState<{ form: RegisteredForm; versionId: string } | null>(null);

  if (forms === null) return <CircularProgress size={24} />;
  return (
    <Box>
      {canWrite && (
        <Button variant="contained" startIcon={<UploadFileIcon />} sx={{ mb: 2 }} onClick={() => setImporting('new')}>
          Import a form
        </Button>
      )}
      {forms.length === 0 ? (
        <Typography color="text.secondary">
          No registered forms yet. Import a fillable PDF, or a template with a field-position file.
        </Typography>
      ) : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table aria-label="Registered forms">
            <TableHead>
              <TableRow>
                <TableCell>Form</TableCell><TableCell>Versions</TableCell><TableCell>Pages</TableCell>
                <TableCell>Fields</TableCell><TableCell>Where it came from</TableCell><TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {forms.map((form) => (
                <TableRow key={form.id} hover>
                  <TableCell>{form.name}</TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                      {form.versions.map((version) => (
                        <Chip key={version.id} size="small" label={`v${version.number}`} clickable
                          color={open?.versionId === version.id ? 'primary' : 'default'}
                          onClick={() => setOpen({ form, versionId: version.id })} />
                      ))}
                    </Stack>
                  </TableCell>
                  <TableCell>{form.latest?.pages ?? '-'}</TableCell>
                  <TableCell>{form.latest?.fields ?? '-'}</TableCell>
                  <TableCell>{form.latest?.source_text ?? '-'}</TableCell>
                  <TableCell align="right">
                    <Stack direction="row" spacing={1} justifyContent="flex-end">
                      <Button size="small" onClick={() => form.latest && setOpen({ form, versionId: form.latest.id })}>View</Button>
                      <Button size="small" onClick={() => onSend(form)}>Fill in and send</Button>
                      {canWrite && form.origin === 'local' && (
                        <Button size="small" onClick={() => setImporting(form)}>New version</Button>
                      )}
                    </Stack>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {open && (
        <Paper sx={{ mt: 3, p: 2, borderRadius: 2 }}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
            <Typography variant="h6" component="h2">
              {open.form.name} v{open.form.versions.find((version) => version.id === open.versionId)?.number}
            </Typography>
            <Button size="small" onClick={() => setOpen(null)}>Close</Button>
          </Stack>
          <VersionDetail client={client} versionId={open.versionId} />
        </Paper>
      )}
      {importing !== null && (
        <ImportDialog client={client} form={importing === 'new' ? null : importing} onClose={() => setImporting(null)}
          onImported={(message) => { setImporting(null); onChanged(message); }} />
      )}
    </Box>
  );
}

// -- Filling in and sending ---------------------------------------------------------------------

function readPicture(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] ?? '');
    reader.onerror = () => reject(new Error('Faxbot could not read that picture.'));
    reader.readAsDataURL(file);
  });
}

function FieldInput({ field, value, onChange }: {
  field: FormField;
  value: FormValue | undefined;
  onChange: (value: FormValue | undefined) => void;
}) {
  const label = field.required ? `${field.label} (required)` : field.label;
  if (field.type === 'checkbox') {
    return <FormControlLabel label={field.label}
      control={<Checkbox checked={value === true} onChange={(event) => onChange(event.target.checked || undefined)} />} />;
  }
  if (field.type === 'choice') {
    return (
      <TextField select label={label} value={typeof value === 'string' ? value : ''} fullWidth
        onChange={(event) => onChange(event.target.value || undefined)}>
        <MenuItem value="">None</MenuItem>
        {(field.options ?? []).map((option) => <MenuItem key={option} value={option}>{option}</MenuItem>)}
      </TextField>
    );
  }
  if (field.type === 'signature') {
    const chosen = value !== undefined;
    return (
      <Stack direction="row" spacing={1} alignItems="center">
        <Button variant="outlined" component="label">
          {chosen ? `${field.label}: picture chosen` : `${field.label}: choose a picture`}
          <input hidden type="file" accept="image/png,image/jpeg,image/gif" aria-label={field.label}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void readPicture(file).then((picture) => onChange({ picture }));
            }} />
        </Button>
        {chosen && <Button size="small" onClick={() => onChange(undefined)}>Remove</Button>}
      </Stack>
    );
  }
  const text = typeof value === 'string' ? value : '';
  return (
    <TextField label={label} value={text} fullWidth multiline={field.multiline} minRows={field.multiline ? 3 : undefined}
      type={field.type === 'date' ? 'date' : 'text'} InputLabelProps={field.type === 'date' ? { shrink: true } : undefined}
      inputProps={{ inputMode: field.type === 'number' ? 'decimal' : undefined, maxLength: field.max_length ?? undefined }}
      helperText={field.type === 'number' && field.decimals ? `Up to ${plural(field.decimals, 'decimal place')}.` : undefined}
      onChange={(event) => onChange(event.target.value === '' ? undefined : event.target.value)} />
  );
}

function SendForm({ client, forms, chosenFormId, canSend, partners, onSent }: {
  client: AdminAPIClient;
  forms: RegisteredForm[];
  chosenFormId: string | null;
  canSend: boolean;
  // Verified partners, when this person may read them; null otherwise.
  partners: DirectPartner[] | null;
  onSent: (delivery: FormDelivery) => void;
}) {
  const [formId, setFormId] = useState<string>(chosenFormId ?? forms[0]?.id ?? '');
  const [versionId, setVersionId] = useState<string>('');
  const [detail, setDetail] = useState<FormVersionDetail | null>(null);
  const [values, setValues] = useState<Record<string, FormValue>>({});
  const [to, setTo] = useState('');
  const [asFax, setAsFax] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewPage, setPreviewPage] = useState(1);
  const [held, setHeld] = useState<PartnerForms | null>(null);
  const [busy, setBusy] = useState<'preview' | 'send' | 'ask' | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { if (chosenFormId) setFormId(chosenFormId); }, [chosenFormId]);
  const form = forms.find((item) => item.id === formId) ?? null;
  useEffect(() => { setVersionId(form?.latest?.id ?? ''); }, [form?.id, form?.latest?.id]);
  useEffect(() => {
    setDetail(null);
    setValues({});
    setPreview(null);
    if (!versionId) return;
    let cancelled = false;
    client.getFormVersion(versionId).then((found) => { if (!cancelled) setDetail(found); }).catch(setError);
    return () => { cancelled = true; };
  }, [client, versionId]);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);

  const partner = useMemo(() => {
    const wanted = digits(to);
    if (!wanted || !partners) return null;
    return partners.find((item) => item.state === 'verified' && digits(item.fax_number).endsWith(wanted)
      && wanted.length >= 10) ?? null;
  }, [partners, to]);
  useEffect(() => setHeld(null), [partner?.id]);

  const change = (name: string, value: FormValue | undefined) => {
    setPreview(null);
    setValues((current) => {
      const next = { ...current };
      if (value === undefined) delete next[name];
      else next[name] = value;
      return next;
    });
  };

  const showPreview = async (page = previewPage) => {
    setBusy('preview');
    setError(null);
    try {
      const blob = await client.renderForm(versionId, values, 'png', page);
      setPreviewPage(page);
      setPreview(URL.createObjectURL(blob));
    } catch (failure) {
      setPreview(null);
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  // Never retried here: a lost answer may mean the form went.
  const send = async () => {
    setBusy('send');
    setError(null);
    try {
      const delivery = await client.sendForm({ version_id: versionId, to: to.trim(), values, route: asFax ? 'fax' : 'auto' });
      setValues({});
      setPreview(null);
      onSent(delivery);
    } catch (failure) {
      setError(failure instanceof TypeError ? new Error(UNCONFIRMED) : failure);
    } finally {
      setBusy(null);
    }
  };

  const askPartner = async () => {
    if (!partner) return;
    setBusy('ask');
    try {
      setHeld(await client.getPartnerForms(partner.id));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  if (forms.length === 0) return <Typography color="text.secondary">Import a form first, on the Forms tab.</Typography>;
  return (
    <Stack spacing={2} sx={{ maxWidth: 720 }}>
      <FormsError error={error} onClose={() => setError(null)} />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <TextField select label="Form" value={formId} onChange={(event) => setFormId(event.target.value)} sx={{ minWidth: 240 }}>
          {forms.map((item) => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}
        </TextField>
        {form && form.versions.length > 1 && (
          <TextField select label="Version" value={versionId} onChange={(event) => setVersionId(event.target.value)}>
            {form.versions.map((version) => <MenuItem key={version.id} value={version.id}>v{version.number}</MenuItem>)}
          </TextField>
        )}
      </Stack>
      {!detail ? <CircularProgress size={24} /> : detail.field_list.map((field) => (
        <FieldInput key={field.name} field={field} value={values[field.name]} onChange={(value) => change(field.name, value)} />
      ))}
      <TextField label="Fax number to send to" value={to} onChange={(event) => setTo(event.target.value)} />
      {partner && (
        <Alert severity="info" sx={{ borderRadius: 2 }}>
          {partner.organization} runs Faxbot: Faxbot sends only the filled-in values, and their Faxbot draws the same
          pages and checks them, page for page. If anything differs, nothing is filed and you decide whether to fax the pages.
          <Box sx={{ mt: 1 }}>
            <Button size="small" disabled={busy !== null} onClick={() => void askPartner()}>Which forms do they hold?</Button>
            {held && <Typography variant="body2" sx={{ mt: 1 }}>
              {held.message}{held.reached && held.forms.length > 0 ? ` ${held.forms.map((item) => `${item.title}${item.version ? ` v${item.version}` : ''}`).join(', ')}.` : ''}
              {' '}A form they lack is fetched from you the first time you send it.
            </Typography>}
          </Box>
          <FormControlLabel sx={{ mt: 1 }} control={<Switch checked={asFax} onChange={(event) => setAsFax(event.target.checked)} />}
            label="Send the pages as a fax instead" />
        </Alert>
      )}
      <Stack direction="row" spacing={2}>
        <Button variant="outlined" disabled={!detail || busy !== null} onClick={() => void showPreview(1)}>
          {busy === 'preview' ? 'Drawing…' : 'Preview'}
        </Button>
        {canSend && (
          <Button variant="contained" disabled={!detail || !to.trim() || busy !== null} onClick={() => void send()}>
            {busy === 'send' ? 'Sending…' : 'Send'}
          </Button>
        )}
      </Stack>
      {preview && detail && (
        <Box>
          {detail.pages > 1 && (
            <TextField select size="small" label="Page" value={previewPage} sx={{ mb: 1 }}
              onChange={(event) => void showPreview(Number(event.target.value))}>
              {Array.from({ length: detail.pages }, (_, index) => <MenuItem key={index} value={index + 1}>{index + 1}</MenuItem>)}
            </TextField>
          )}
          <Typography variant="caption" color="text.secondary" display="block">
            Page {previewPage} exactly as it is faxed. Nothing has been sent.
          </Typography>
          <Box component="img" src={preview} alt={`Filled page ${previewPage}`}
            sx={{ maxWidth: '100%', border: '1px solid', borderColor: 'divider', borderRadius: 1, bgcolor: 'common.white' }} />
        </Box>
      )}
    </Stack>
  );
}

// -- Sent forms ---------------------------------------------------------------------------------

function SentForms({ client, deliveries, canSend, onChanged }: {
  client: AdminAPIClient;
  deliveries: FormDelivery[] | null;
  canSend: boolean;
  onChanged: (message: string) => void;
}) {
  const [confirming, setConfirming] = useState<FormDelivery | null>(null);
  const [opened, setOpened] = useState<FormDelivery | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const fax = async (delivery: FormDelivery) => {
    setBusy(true);
    setError(null);
    try {
      const result = await client.faxFormDelivery(delivery.id);
      setConfirming(null);
      onChanged(result.message ?? 'The pages are on their way as a fax.');
    } catch (failure) {
      setError(failure instanceof TypeError ? new Error(UNCONFIRMED) : failure);
    } finally {
      setBusy(false);
    }
  };

  if (deliveries === null) return <CircularProgress size={24} />;
  return (
    <Box>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {deliveries.length === 0 ? <Typography color="text.secondary">No forms sent yet.</Typography> : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table aria-label="Sent forms">
            <TableHead>
              <TableRow>
                <TableCell>Sent</TableCell><TableCell>Form</TableCell><TableCell>To</TableCell>
                <TableCell>What happened</TableCell><TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {deliveries.map((delivery) => (
                <TableRow key={delivery.id}>
                  <TableCell>{formatServerTime(delivery.created_at)}</TableCell>
                  <TableCell>{delivery.form ?? 'Form'}{delivery.form_version ? ` v${delivery.form_version}` : ''}</TableCell>
                  <TableCell>{delivery.partner ?? delivery.fax_number}</TableCell>
                  <TableCell sx={{ maxWidth: 360 }}>
                    <Typography variant="body2">{delivery.status}</Typography>
                    {delivery.fax_id && delivery.route === 'direct' && (
                      <Typography variant="caption" color="text.secondary">You sent the pages as a fax afterwards.</Typography>
                    )}
                  </TableCell>
                  <TableCell align="right">
                    <Stack direction="row" spacing={1} justifyContent="flex-end">
                      <Button size="small" onClick={() => void client.getFormDelivery(delivery.id).then(setOpened).catch(setError)}>
                        Values
                      </Button>
                      {canSend && delivery.can_fax && (
                        <Button size="small" variant="outlined" onClick={() => setConfirming(delivery)}>Send the pages as a fax</Button>
                      )}
                    </Stack>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      <Dialog open={Boolean(confirming)} onClose={() => setConfirming(null)} fullWidth maxWidth="sm">
        <DialogTitle>Send the pages as a fax?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            Faxbot faxes the same filled-in pages to {confirming?.fax_number}. This is an ordinary fax and costs what a fax
            to that number costs. It is sent once.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirming(null)}>Cancel</Button>
          <Button variant="contained" disabled={busy} onClick={() => confirming && void fax(confirming)}>
            {busy ? 'Sending…' : 'Send as a fax'}
          </Button>
        </DialogActions>
      </Dialog>
      <Dialog open={Boolean(opened)} onClose={() => setOpened(null)} fullWidth maxWidth="sm">
        <DialogTitle>{opened?.form ?? 'Form'}{opened?.form_version ? ` v${opened.form_version}` : ''} to {opened?.partner ?? opened?.fax_number}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{opened?.status}</Typography>
          {opened && (opened.can_open_values === false
            ? <Typography variant="body2">You can see that this form was sent, but opening what was filled in needs access to this fax's document.</Typography>
            : <FormValuesTable form={{ values: opened.values ?? null, fields: opened.fields ?? [] }} />)}
        </DialogContent>
        <DialogActions><Button onClick={() => setOpened(null)}>Close</Button></DialogActions>
      </Dialog>
    </Box>
  );
}

export default function Forms({ client, canWrite, canSend, canReadSettings }: {
  client: AdminAPIClient;
  // Import forms and new versions (settings:write).
  canWrite: boolean;
  // Send filled-in forms (fax:send).
  canSend: boolean;
  // Read the forms sent and the partner list (settings:read); a fax operator fills in and sends only.
  canReadSettings: boolean;
}) {
  const [tab, setTab] = useState<FormsTab>('forms');
  const [forms, setForms] = useState<RegisteredForm[] | null>(null);
  const [deliveries, setDeliveries] = useState<FormDelivery[] | null>(null);
  const [partners, setPartners] = useState<DirectPartner[] | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      setForms((await client.listForms()).forms);
    } catch (failure) {
      setForms([]);
      setError(failure);
    }
    if (!canReadSettings) return;
    try {
      setDeliveries((await client.listFormDeliveries()).deliveries);
    } catch {
      setDeliveries([]);
    }
  }, [client, canReadSettings]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!canReadSettings) return;
    client.listDirectPartners().then((result) => setPartners(result.peers)).catch(() => setPartners(null));
  }, [client, canReadSettings]);

  const sent = (delivery: FormDelivery) => {
    setNotice(delivery.route === 'fax' ? 'The form is on its way as a fax.' : delivery.status);
    if (canReadSettings) setTab('sent');
    void load();
  };

  return (
    <Box>
      <ScreenHeader title="Forms" onRefresh={() => void load()}
        subtitle="Forms you fill in and send. A partner running Faxbot gets only the filled-in values and draws identical pages itself; anyone else gets the pages as a fax." />
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Tabs value={tab} onChange={(_, next: FormsTab) => setTab(next)} sx={{ mb: 3 }}>
        <Tab value="forms" label="Forms" />
        <Tab value="send" label="Send a form" />
        {canReadSettings && <Tab value="sent" label="Sent forms" />}
      </Tabs>
      {tab === 'forms' && (
        <FormsList client={client} forms={forms} canWrite={canWrite}
          onChanged={(message) => { setNotice(message); void load(); }}
          onSend={(form) => { setChosen(form.id); setTab('send'); }} />
      )}
      {tab === 'send' && (forms === null ? <CircularProgress size={24} /> : (
        <SendForm client={client} forms={forms} chosenFormId={chosen} canSend={canSend} partners={partners} onSent={sent} />
      ))}
      {tab === 'sent' && (
        <SentForms client={client} deliveries={deliveries} canSend={canSend}
          onChanged={(message) => { setNotice(message); void load(); }} />
      )}
    </Box>
  );
}
