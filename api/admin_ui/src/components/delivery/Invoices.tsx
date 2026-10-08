// Costs → Invoices: enter each provider account's monthly invoice total, optionally with the invoice itself, and see
// the part your faxes don't explain. Entering the same month again adds a corrected version and keeps the earlier one.
// A fax with no price is counted, never priced at zero, so a month with one reads as incomplete.
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, MenuItem, Paper, Stack, Table, TableBody,
  TableCell, TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { InvoiceDetail, InvoicesView } from '../../api/chargesTypes';
import { formatLocalDate, formatServerTime } from '../../api/time';
import { LoadStateView, ScreenHeader, loadFailure, type LoadState } from '../access/AccessViews';
import { DeliveryError, Notice, formatMoney } from './shared';

// Last month, as the month field writes it ("2026-09").
export function lastMonth(today = new Date()): string {
  const month = new Date(today.getFullYear(), today.getMonth() - 1, 1);
  return `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, '0')}`;
}

function billingDayText(day: number): string {
  if (day <= 1) return 'from the 1st of the month';
  const suffix = day % 100 >= 11 && day % 100 <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[day % 10] ?? 'th';
  return `from the ${day}${suffix}, the plan's billing day`;
}

function InvoiceDialog({ client, invoiceId, onClose }: { client: AdminAPIClient; invoiceId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<InvoiceDetail | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    client.getInvoice(invoiceId).then(setDetail, setError);
  }, [client, invoiceId]);

  const download = async () => {
    if (!detail) return;
    try {
      const blob = await client.downloadInvoiceFile(detail.id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = detail.file?.name || 'invoice';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (failure) {
      setError(failure);
    }
  };

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm" aria-labelledby="invoice-title">
      <DialogTitle id="invoice-title">{detail ? `${detail.label}, ${detail.period_name}` : 'Invoice'}</DialogTitle>
      <DialogContent>
        <DeliveryError error={error} onClose={() => setError(null)} />
        {!detail ? <LoadStateView state="loading" /> : (
          <Stack spacing={2}>
            <Typography>{detail.summary}</Typography>
            <Typography variant="body2" color="text.secondary">
              Invoice {formatMoney(detail.total)}; your faxes explain {formatMoney(detail.explained)}.
              {' '}Covers {formatLocalDate(detail.first_day)} to {formatLocalDate(detail.last_day)}.
            </Typography>
            {detail.parts.length > 0 && (
              <Table size="small" aria-label="What your faxes explain">
                <TableBody>
                  {detail.parts.map((part) => (
                    <TableRow key={part.label}>
                      <TableCell>{part.label}</TableCell>
                      <TableCell align="right">{formatMoney(part.amount)}</TableCell>
                    </TableRow>
                  ))}
                  <TableRow>
                    <TableCell><strong>Not explained by your faxes</strong></TableCell>
                    <TableCell align="right"><strong>{formatMoney(detail.residual)}</strong></TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            )}
            {detail.notes.map((note) => <Typography key={note} variant="body2">{note}</Typography>)}
            {detail.note && <Typography variant="body2">Note: {detail.note}</Typography>}
            {!detail.current && (
              <Alert severity="info" sx={{ borderRadius: 2 }}>A later correction replaced this total.</Alert>
            )}
            {detail.history.length > 1 && (
              <Box>
                <Typography variant="subtitle2" component="h3">Totals entered</Typography>
                <Table size="small" aria-label="Totals entered">
                  <TableBody>
                    {detail.history.map((entry) => (
                      <TableRow key={entry.id}>
                        <TableCell>{formatMoney(entry.total)}</TableCell>
                        <TableCell>{entry.entered_by ?? '-'}</TableCell>
                        <TableCell>{formatServerTime(entry.entered_at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        {detail?.file && <Button onClick={() => void download()}>Download the invoice file</Button>}
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}

export default function Invoices({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [state, setState] = useState<LoadState>('loading');
  const [data, setData] = useState<InvoicesView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [account, setAccount] = useState('');
  const [month, setMonth] = useState(lastMonth());
  const [total, setTotal] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [note, setNote] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      const view = await client.listInvoices();
      setData(view);
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const chosen = useMemo(() => data?.accounts.find((item) => item.account_key === account) ?? null, [data, account]);
  useEffect(() => {
    if (chosen) setCurrency(chosen.currency);
  }, [chosen]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const entered = await client.addInvoice({ account, month, total: total.trim(), currency, note: note.trim(), file });
      setNotice(entered.summary);
      setTotal('');
      setNote('');
      setFile(null);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Box>
      <ScreenHeader title="Invoices" onRefresh={() => void load()} busy={state === 'loading'}
        subtitle="Enter each provider's monthly invoice, and see the part your faxes don't explain." />
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      {state !== 'ready' || !data ? <LoadStateView state={state} onRetry={() => void load()} /> : (
        <Stack spacing={4}>
          {data.recommendations.map((advice) => (
            <Alert key={advice.account_key} severity="warning" sx={{ borderRadius: 2 }}>{advice.text}</Alert>
          ))}

          {canWrite && (
            <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} component="section" aria-labelledby="enter-invoice">
              <Typography variant="h6" component="h2" id="enter-invoice">Enter an invoice</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                Type the total from the provider's invoice. Entering the same month again corrects it and keeps the
                earlier total.
              </Typography>
              <Box display="grid" gap={2} sx={{ gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
                <TextField select label="Account" value={account} onChange={(event) => setAccount(event.target.value)}
                  helperText={chosen ? `The month counts ${billingDayText(chosen.billing_day)}.` : ' '}>
                  {data.accounts.map((item) => (
                    <MenuItem key={item.account_key} value={item.account_key}>{item.label}</MenuItem>
                  ))}
                </TextField>
                <TextField label="Month" type="month" value={month} onChange={(event) => setMonth(event.target.value)}
                  InputLabelProps={{ shrink: true }} />
                <TextField label="Invoice total" value={total} onChange={(event) => setTotal(event.target.value)}
                  placeholder="13.20" inputProps={{ inputMode: 'decimal' }} />
                <TextField label="Currency" value={currency} onChange={(event) => setCurrency(event.target.value.toUpperCase())}
                  inputProps={{ maxLength: 3 }} />
                <TextField label="Note (optional)" value={note} onChange={(event) => setNote(event.target.value)}
                  placeholder="Invoice number" inputProps={{ maxLength: 500 }} />
                <Box display="flex" alignItems="center" gap={1}>
                  <Button variant="outlined" component="label">
                    {file ? 'Change the file' : 'Attach the invoice'}
                    <input hidden type="file" accept=".pdf,.png,.jpg,.jpeg,.csv,application/pdf,image/png,image/jpeg,text/csv"
                      aria-label="Invoice file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
                  </Button>
                  <Typography variant="body2" color="text.secondary" noWrap>{file ? file.name : 'PDF, picture or CSV'}</Typography>
                </Box>
              </Box>
              <Box mt={2}>
                <Button variant="contained" onClick={() => void save()} disabled={saving || !account || !total.trim() || !month}>
                  {saving ? 'Saving…' : 'Save the invoice'}
                </Button>
              </Box>
            </Paper>
          )}

          <Box component="section">
            <Typography variant="h6" component="h2">Invoices you entered</Typography>
            {data.invoices.length === 0 ? (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                No invoices yet. Providers that report no charge per fax, like HumbleFax and eFax, are only
                accounted for once you enter their invoice.
              </Typography>
            ) : (
              <TableContainer component={Paper} variant="outlined" sx={{ mt: 1, borderRadius: 2 }}>
                <Table size="small" aria-label="Invoices you entered">
                  <TableHead>
                    <TableRow>
                      <TableCell>Account</TableCell>
                      <TableCell>Month</TableCell>
                      <TableCell align="right">Invoice</TableCell>
                      <TableCell align="right">Explained by your faxes</TableCell>
                      <TableCell align="right">Not explained</TableCell>
                      <TableCell />
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {data.invoices.map((invoice) => (
                      <TableRow key={invoice.id} data-testid="invoice-row">
                        <TableCell>{invoice.label}</TableCell>
                        <TableCell>{invoice.period_name}</TableCell>
                        <TableCell align="right">{formatMoney(invoice.total)}</TableCell>
                        <TableCell align="right">{formatMoney(invoice.explained)}</TableCell>
                        <TableCell align="right">
                          {formatMoney(invoice.residual)}{invoice.complete ? '' : ' (some faxes have no price)'}
                        </TableCell>
                        <TableCell align="right">
                          <Button size="small" onClick={() => setOpen(invoice.id)}
                            aria-label={`Details of the ${invoice.label} invoice for ${invoice.period_name}`}>Details</Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
          </Box>
        </Stack>
      )}
      {open && <InvoiceDialog client={client} invoiceId={open} onClose={() => setOpen(null)} />}
    </Box>
  );
}
