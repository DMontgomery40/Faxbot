// On a received fax that a partner sent as registered form data: which form it was
// drawn from, and the typed values that came with it.
import { useState } from 'react';
import { Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Table, TableBody, TableCell, TableRow,
  Typography } from '@mui/material';
import type { FormValue, ReceivedForm } from '../../api/formsTypes';

// A typed value as people read it: dates in the reader's own format, never a code.
export function valueText(value: FormValue | undefined, type?: string): string {
  if (value === undefined || value === null || value === '') return '-';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') return 'Signature picture';
  if (type === 'date' && /^\d{4}-\d\d-\d\d$/.test(value)) {
    const [year, month, day] = value.split('-').map(Number);
    return new Date(year, month - 1, day).toLocaleDateString();
  }
  return value;
}

export function formSentence(form: Pick<ReceivedForm, 'form' | 'form_version'>): string {
  return `Rendered from ${form.form ?? 'a registered form'}${form.form_version ? ` v${form.form_version}` : ''}; values attached.`;
}

export function FormValuesTable({ form }: { form: Pick<ReceivedForm, 'values' | 'fields'> }) {
  const values = form.values ?? {};
  const fields = form.fields?.length ? form.fields : Object.keys(values).map((name) => ({ name, label: name, type: 'text' as const }));
  const filled = fields.filter((field) => values[field.name] !== undefined);
  if (filled.length === 0) return <Typography variant="body2">No values were filled in.</Typography>;
  return (
    <Table size="small" aria-label="Form values">
      <TableBody>
        {filled.map((field) => (
          <TableRow key={field.name}>
            <TableCell component="th" scope="row" sx={{ fontWeight: 600 }}>{field.label}</TableCell>
            <TableCell sx={{ whiteSpace: 'pre-wrap' }}>{valueText(values[field.name], field.type)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export default function ReceivedFormLine({ form }: { form: ReceivedForm }) {
  const [open, setOpen] = useState(false);
  return (
    <Box onClick={(event) => event.stopPropagation()}>
      <Typography variant="caption" color="text.secondary" display="block">{formSentence(form)}</Typography>
      <Button size="small" onClick={() => setOpen(true)} sx={{ px: 0, minWidth: 0 }}>Show the values</Button>
      <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>{form.form ?? 'Form'}{form.form_version ? ` v${form.form_version}` : ''} from {form.partner ?? 'a partner'}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Faxbot drew these pages from the values below and checked that they match, page for page, what the sender would have faxed.
          </Typography>
          <FormValuesTable form={form} />
        </DialogContent>
        <DialogActions><Button onClick={() => setOpen(false)}>Close</Button></DialogActions>
      </Dialog>
    </Box>
  );
}
