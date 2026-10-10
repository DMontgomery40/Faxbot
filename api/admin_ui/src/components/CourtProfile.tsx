// The California court profile (api/app/court): Delivery setup → Routing rules (where it applies, the courts and
// rules it rests on), Send a fax (the case details), and Sent details (what it did, timing, the transmission record).
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Checkbox, FormControlLabel, Link, MenuItem, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import type AdminAPIClient from '../api/client';
import { AdminAPIError } from '../api/client';
import type { CourtDetails, CourtFax, CourtProfile } from '../api/courtTypes';

type Client = Pick<AdminAPIClient, 'courtProfile' | 'setCourtAttachment' | 'courtDeadline'>;

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

// A source's read date in the reader's own words ('2026-10-10' is a calendar day, not a moment).
function readOn(day: string): string {
  const [year, month, date] = day.split('-').map(Number);
  if (!year || !month || !date) return day;
  return new Date(year, month - 1, date).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

const STATUS_WORDS = { accepts: 'Takes fax filing', stopped: 'Stopped', does_not_accept: 'Refuses' } as const;

// Delivery setup → Routing rules: the California court profile.
export function CourtProfilePanel({ client, canWrite }: { client: Client; canWrite: boolean }) {
  const [view, setView] = useState<CourtProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState<'mailbox' | 'rule'>('mailbox');
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const [deadline, setDeadline] = useState('');
  const [extended, setExtended] = useState<string | null>(null);

  const load = useCallback(() => {
    client.courtProfile().then((found) => { setView(found); setError(null); })
      .catch((failure) => setError(problem(failure, 'The court profile could not be loaded. Try again.')));
  }, [client]);
  useEffect(() => { load(); }, [load]);

  const change = async (nextKind: 'mailbox' | 'rule', nextTarget: string, on: boolean) => {
    setBusy(true);
    try {
      const found = await client.setCourtAttachment(nextKind, nextTarget, on);
      setView(found);
      setTarget('');
      setError(null);
    } catch (failure) {
      setError(problem(failure, 'This could not be saved. Try again.'));
    } finally {
      setBusy(false);
    }
  };

  const extend = async () => {
    try {
      setExtended((await client.courtDeadline(deadline)).sentence);
    } catch (failure) {
      setExtended(problem(failure, 'Faxbot could not count court days for that date.'));
    }
  };

  if (!view) {
    return error ? <Alert severity="warning" sx={{ mt: 4 }}>{error}</Alert> : null;
  }
  const choices = kind === 'mailbox'
    ? view.mailboxes.map((item) => ({ id: item.id, name: item.label }))
    : view.routing_rules;
  return (
    <Card variant="outlined" sx={{ mt: 4, borderRadius: 2 }} data-testid="court-profile">
      <CardContent>
        <Typography variant="h6" component="h2">California court profile</Typography>
        <Typography variant="body2" sx={{ mb: 1 }}>{view.summary}</Typography>
        {error && <Alert severity="warning" sx={{ mb: 1 }}>{error}</Alert>}
        {view.attached.length === 0 && (
          <Typography variant="body2" color="text.secondary">
            It applies to no mailbox or routing rule yet. A sender can still choose it for one fax on Send a fax.
          </Typography>
        )}
        {view.attached.map((item) => (
          <Stack key={`${item.kind}-${item.target}`} direction="row" spacing={1} alignItems="center">
            <Typography variant="body2">{item.sentence}</Typography>
            {canWrite && (
              <Button size="small" disabled={busy} onClick={() => void change(item.kind, item.target, false)}>
                Take it off
              </Button>
            )}
          </Stack>
        ))}
        {canWrite && (
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }} alignItems={{ sm: 'center' }}>
            <TextField select size="small" label="Apply it to" value={kind} sx={{ minWidth: 160 }}
              onChange={(event) => { setKind(event.target.value as 'mailbox' | 'rule'); setTarget(''); }}>
              <MenuItem value="mailbox">A mailbox</MenuItem>
              <MenuItem value="rule">A routing rule</MenuItem>
            </TextField>
            <TextField select size="small" label={kind === 'mailbox' ? 'Mailbox' : 'Routing rule'} value={target}
              sx={{ minWidth: 220 }} onChange={(event) => setTarget(event.target.value)}
              inputProps={{ 'data-testid': 'court-target' }}>
              {choices.map((item) => <MenuItem key={item.id} value={item.id}>{item.name}</MenuItem>)}
            </TextField>
            <Button variant="outlined" disabled={busy || !target} onClick={() => void change(kind, target, true)}>
              Apply the court profile
            </Button>
          </Stack>
        )}
        <Typography variant="body2" sx={{ mt: 2 }}>{view.trend}</Typography>
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small" aria-label="California courts and fax filing">
            <TableHead>
              <TableRow>
                <TableCell>County</TableCell><TableCell>Fax filing</TableCell><TableCell>What its rule says</TableCell>
                <TableCell>Fax number</TableCell><TableCell>Source</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {view.courts.map((item) => (
                <TableRow key={item.county}>
                  <TableCell>{item.county}</TableCell>
                  <TableCell>{STATUS_WORDS[item.status]}</TableCell>
                  <TableCell>{item.note}</TableCell>
                  <TableCell>{item.fax ?? '-'}</TableCell>
                  <TableCell>
                    <Link href={item.source} target="_blank" rel="noreferrer">{new URL(item.source).hostname}</Link>
                    {`, read ${readOn(item.read_on)}`}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
          {`Not established either way: ${view.not_established.join(', ')}. Their own local rule decides.`}
        </Typography>
        <Typography variant="subtitle2" sx={{ mt: 2 }}>The rules it applies</Typography>
        {view.rules.map((rule) => (
          <Typography key={rule.rule} variant="body2">
            {`${rule.rule}: ${rule.says} `}
            <Link href={rule.url} target="_blank" rel="noreferrer">Read it</Link>{`, read ${readOn(rule.read_on)}.`}
          </Typography>
        ))}
        <Typography variant="body2" sx={{ mt: 1 }}>
          {`Court days count the published court holidays for ${view.holidays.years.join(' and ')}. `}
          <Link href={view.holidays.source.url} target="_blank" rel="noreferrer">Court holidays</Link>
          {`, read ${readOn(view.holidays.source.read_on)}.`}
        </Typography>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 2 }} alignItems={{ sm: 'center' }}>
          <TextField type="date" size="small" label="A deadline after service by fax" value={deadline}
            onChange={(event) => setDeadline(event.target.value)} InputLabelProps={{ shrink: true }}
            inputProps={{ 'data-testid': 'court-deadline' }} />
          <Button variant="outlined" disabled={!deadline} onClick={() => void extend()}>Add two court days</Button>
        </Stack>
        {extended && <Typography variant="body2" sx={{ mt: 1 }}>{extended}</Typography>}
      </CardContent>
    </Card>
  );
}

// Send a fax: the case details for a court fax. `value` null means an ordinary fax.
export const EMPTY_COURT: CourtDetails = {
  purpose: 'file', county: '', case_number: '', plaintiff: '', defendant: '', documents: [{ title: '', pages: 1 }],
  sender: ['', '', '', ''], telephone: '', attorney_for: '', filing_fee: false, fee_account: '',
  by_fax_confirmed: false, agreement_confirmed: false, number_confirmed: false, served_party: '', fax: '',
};

// The JSON POST /fax takes: empty lines and fields left out, never a payment card.
export function courtJson(value: CourtDetails): string {
  const sender = value.sender.map((line) => line.trim()).filter(Boolean);
  const body: Record<string, unknown> = {
    purpose: value.purpose, county: value.county.trim(), case_number: value.case_number.trim(),
    plaintiff: value.plaintiff.trim(), defendant: value.defendant.trim(),
    documents: value.documents.filter((item) => item.title.trim()).map((item) => ({ title: item.title.trim(), pages: item.pages })),
  };
  if (value.purpose === 'file') {
    Object.assign(body, { sender, telephone: value.telephone.trim(), attorney_for: value.attorney_for.trim(),
      filing_fee: value.filing_fee, fee_account: value.fee_account.trim(), by_fax_confirmed: value.by_fax_confirmed });
  } else {
    Object.assign(body, { served_party: value.served_party.trim(), fax: value.fax.trim(),
      agreement_confirmed: value.agreement_confirmed, number_confirmed: value.number_confirmed });
  }
  return JSON.stringify(body);
}

export function CourtSendFields({ value, onChange, disabled }: {
  value: CourtDetails | null; onChange: (next: CourtDetails | null) => void; disabled?: boolean;
}) {
  const set = (patch: Partial<CourtDetails>) => value && onChange({ ...value, ...patch });
  return (
    <Box data-testid="send-court" sx={{ mt: 1 }}>
      <FormControlLabel
        control={<Checkbox checked={value !== null} disabled={disabled}
          onChange={(event) => onChange(event.target.checked ? EMPTY_COURT : null)} />}
        label="Filing with a California court, or serving a party, by fax" />
      {value && (
        <Stack spacing={1} sx={{ mt: 1, maxWidth: 640 }}>
          <Typography variant="body2" color="text.secondary">
            The pages go exactly as you give them, by a fax call. Filing adds the MC-005 cover sheet first; each
            document must say &quot;By fax&quot; immediately below its title.
          </Typography>
          <TextField select size="small" label="Purpose" value={value.purpose} disabled={disabled}
            onChange={(event) => set({ purpose: event.target.value as 'file' | 'serve' })}>
            <MenuItem value="file">File with the court (rule 2.304)</MenuItem>
            <MenuItem value="serve">Serve a party (rule 2.306)</MenuItem>
          </TextField>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <TextField size="small" label="County" value={value.county} disabled={disabled}
              onChange={(event) => set({ county: event.target.value })} inputProps={{ 'data-testid': 'court-county' }} />
            <TextField size="small" label="Case number" value={value.case_number} disabled={disabled}
              onChange={(event) => set({ case_number: event.target.value })} />
          </Stack>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <TextField size="small" label="Plaintiff or petitioner" value={value.plaintiff} disabled={disabled}
              onChange={(event) => set({ plaintiff: event.target.value })} />
            <TextField size="small" label="Defendant or respondent" value={value.defendant} disabled={disabled}
              onChange={(event) => set({ defendant: event.target.value })} />
          </Stack>
          {value.documents.map((item, index) => (
            <Stack key={index} direction="row" spacing={1}>
              <TextField size="small" label={`Document ${index + 1} title`} value={item.title} disabled={disabled}
                sx={{ flex: 1 }} onChange={(event) => set({ documents: value.documents.map((doc, at) =>
                  at === index ? { ...doc, title: event.target.value } : doc) })} />
              <TextField size="small" type="number" label="Pages" value={item.pages} disabled={disabled}
                sx={{ width: 96 }} inputProps={{ min: 1, max: 999 }}
                onChange={(event) => set({ documents: value.documents.map((doc, at) =>
                  at === index ? { ...doc, pages: Math.max(1, Number(event.target.value) || 1) } : doc) })} />
            </Stack>
          ))}
          <Box>
            <Button size="small" disabled={disabled || value.documents.length >= 15}
              onClick={() => set({ documents: [...value.documents, { title: '', pages: 1 }] })}>Add a document</Button>
          </Box>
          {value.purpose === 'file' ? (
            <>
              {value.sender.map((line, index) => (
                <TextField key={index} size="small" value={line} disabled={disabled}
                  label={['Attorney or party (name and State Bar number)', 'Firm or address', 'Address', 'City, state and ZIP'][index]}
                  onChange={(event) => set({ sender: value.sender.map((old, at) => at === index ? event.target.value : old) })} />
              ))}
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                <TextField size="small" label="Telephone" value={value.telephone} disabled={disabled}
                  onChange={(event) => set({ telephone: event.target.value })} />
                <TextField size="small" label="Attorney for" value={value.attorney_for} disabled={disabled}
                  onChange={(event) => set({ attorney_for: event.target.value })} />
              </Stack>
              <FormControlLabel control={<Checkbox checked={value.filing_fee} disabled={disabled}
                onChange={(event) => set({ filing_fee: event.target.checked })} />}
                label="A filing fee is due: charge it to your account with the court" />
              {value.filing_fee && (
                <TextField size="small" label="Your court account number" value={value.fee_account} disabled={disabled}
                  helperText="Faxbot never takes a payment card." onChange={(event) => set({ fee_account: event.target.value })} />
              )}
              <FormControlLabel control={<Checkbox checked={value.by_fax_confirmed} disabled={disabled}
                onChange={(event) => set({ by_fax_confirmed: event.target.checked })} />}
                label='Each document is a scan that says "By fax" below its title (Faxbot checks documents with text itself)' />
            </>
          ) : (
            <>
              <TextField size="small" label="Party or attorney served" value={value.served_party} disabled={disabled}
                onChange={(event) => set({ served_party: event.target.value })} />
              <TextField size="small" label="Your fax number, for the proof of service" value={value.fax}
                disabled={disabled} onChange={(event) => set({ fax: event.target.value })} />
              <FormControlLabel control={<Checkbox checked={value.agreement_confirmed} disabled={disabled}
                onChange={(event) => set({ agreement_confirmed: event.target.checked })} />}
                label="The parties agreed to service by fax, and the agreement is confirmed in writing" />
              <FormControlLabel control={<Checkbox checked={value.number_confirmed} disabled={disabled}
                onChange={(event) => set({ number_confirmed: event.target.checked })} />}
                label="This is the fax number the party last gave on a filed and served document" />
            </>
          )}
        </Stack>
      )}
    </Box>
  );
}

// Sent details: what the court profile did for this fax, when filing or service is deemed to occur, the record.
export function SentCourt({ client, jobId }: {
  client: Pick<AdminAPIClient, 'courtFax' | 'downloadCourtRecord'>; jobId: string;
}) {
  const [view, setView] = useState<CourtFax | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.courtFax(jobId).then((found) => { if (live) setView(found.court); }).catch(() => { if (live) setView(null); });
    return () => { live = false; };
  }, [client, jobId]);
  if (!view) return null;
  const save = async () => {
    try {
      const blob = await client.downloadCourtRecord(jobId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `transmission-record-${jobId.slice(0, 8)}.pdf`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (failure) {
      setMessage(problem(failure, 'The transmission record could not be downloaded. Try again.'));
    }
  };
  return (
    <Alert severity="info" sx={{ mt: 2 }} data-testid="sent-court">
      <Typography variant="body2" fontWeight={600}>
        {`${view.purpose_text}: ${view.case.title}, ${view.case.county} County`}
        {view.case.case_number ? `, case ${view.case.case_number}` : ''}
      </Typography>
      {[...view.sentences, ...view.timing].map((sentence) => (
        <Typography key={sentence} variant="body2">{sentence}</Typography>
      ))}
      {view.court_note && <Typography variant="body2">{view.court_note}</Typography>}
      {view.record_ready && (
        <Button size="small" sx={{ mt: 1 }} onClick={() => void save()}>Download the transmission record</Button>
      )}
      {message && <Typography variant="body2">{message}</Typography>}
    </Alert>
  );
}
