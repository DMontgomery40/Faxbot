// This installation's direct delivery card, shown read-only for copying to a partner.
import { TextField, Typography } from '@mui/material';
import { FormDialog } from '../access/AccessViews';

export default function DirectCardDialog({ card, onClose, onCopied }: {
  card: string | null;
  onClose: () => void;
  onCopied: () => void;
}) {
  return (
    <FormDialog open={card !== null} title="Our direct delivery card" submitLabel="Copy" canSubmit
      onSubmit={() => { void navigator.clipboard?.writeText(card ?? ''); onCopied(); }}
      onClose={onClose}>
      <Typography variant="body2" sx={{ mb: 1 }}>Send this card to a partner. It contains no secrets.</Typography>
      <TextField fullWidth multiline minRows={8} value={card ?? ''} InputProps={{ readOnly: true, sx: { fontFamily: 'monospace', fontSize: 12 } }} />
    </FormDialog>
  );
}
