import { useState } from 'react';
import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  TextField,
  useMediaQuery,
  useTheme,
} from '@mui/material';
import AdminAPIClient, { accessErrorMessage, isNotAvailable } from '../api/client';
import SecretDialog, { type SecretReveal } from './access/SecretDialog';

// First-run: the installation key can create the first named owner.
export default function OwnerEnrollment({ client, onEnrolled }: { client: AdminAPIClient; onEnrolled: () => void }) {
  const theme = useTheme();
  const fullScreen = useMediaQuery(theme.breakpoints.down('sm'));
  const [open, setOpen] = useState(false);
  const [login, setLogin] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reveal, setReveal] = useState<SecretReveal | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await client.enrollOwner({ login: login.trim(), display_name: displayName.trim() });
      setOpen(false);
      setReveal({
        title: 'Owner created',
        message: `Sign in as ${login.trim()} with this temporary password. It is shown only once, and a new password is required at first sign-in.`,
        label: 'Temporary password',
        secret: result.temporary_password,
      });
      setLogin('');
      setDisplayName('');
    } catch (failure) {
      setError(isNotAvailable(failure)
        ? 'Creating the first owner is not available on this server yet.'
        : accessErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Alert
        severity="info"
        sx={{ mb: 2, borderRadius: 2 }}
        action={<Button color="inherit" size="small" onClick={() => setOpen(true)}>Create the first owner</Button>}
      >
        No owner account exists yet. Create one so people can sign in with a username and password.
      </Alert>
      <Dialog open={open} onClose={() => !busy && setOpen(false)} maxWidth="sm" fullWidth fullScreen={fullScreen}>
        <DialogTitle>Create the first owner</DialogTitle>
        <DialogContent>
          {error && <Alert severity="error" sx={{ mb: 2, borderRadius: 2 }}>{error}</Alert>}
          <TextField fullWidth label="Username" value={login} onChange={(e) => setLogin(e.target.value)}
            sx={{ mt: 1 }} autoFocus inputProps={{ maxLength: 128 }} />
          <TextField fullWidth label="Display name" value={displayName} onChange={(e) => setDisplayName(e.target.value)}
            sx={{ mt: 2 }} inputProps={{ maxLength: 128 }} />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 3 }}>
          <Button onClick={() => setOpen(false)} disabled={busy}>Cancel</Button>
          <Button variant="contained" onClick={submit} disabled={busy || !login.trim() || !displayName.trim()}>
            Create owner
          </Button>
        </DialogActions>
      </Dialog>
      <SecretDialog reveal={reveal} onClose={() => { setReveal(null); onEnrolled(); }} />
    </>
  );
}
