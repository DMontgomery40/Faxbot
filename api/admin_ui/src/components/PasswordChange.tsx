import React, { useState } from 'react';
import { Alert, Box, Button, CircularProgress, Container, Paper, TextField, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../api/client';

interface PasswordChangeProps {
  client: AdminAPIClient;
  displayName: string;
  onChanged: () => Promise<void>;
  onSignOut: () => void;
}

function changeError(error: unknown): string {
  if (error instanceof AdminAPIError) {
    if (error.status === 400 || error.status === 422) return 'Check the current password and choose a different new password.';
    if (error.status === 401) return 'The current password is not correct.';
    if (error.status === 429 && error.detail) return error.detail;
    if (error.status === 503 && error.detail) return error.detail;
    return 'The password could not be changed. Try again.';
  }
  if (error instanceof TypeError) return 'Could not reach the server. Check the connection and try again.';
  return 'The password could not be changed. Try again.';
}

export default function PasswordChange({ client, displayName, onChanged, onSignOut }: PasswordChangeProps) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mismatch = confirm.length > 0 && next !== confirm;
  const ready = Boolean(current && next && next === confirm);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      await client.changePassword(current, next);
    } catch (failure) {
      setError(changeError(failure));
      setBusy(false);
      return;
    }
    try {
      await onChanged();
    } catch {
      setError('Your password was changed. Sign in again with the new password.');
      setBusy(false);
    }
  };

  return (
    <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center' }}>
      <Container maxWidth="sm">
        <Paper component="form" onSubmit={submit} noValidate elevation={0}
          sx={{ p: 4, borderRadius: 4, border: '1px solid', borderColor: 'divider' }}>
          <Typography variant="h4" component="h1" gutterBottom sx={{ fontWeight: 600 }}>
            Choose a new password
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {displayName}, you need a new password before you can continue.
          </Typography>
          {error && <Alert severity="error" role="alert" sx={{ mt: 2, borderRadius: 2 }}>{error}</Alert>}
          <TextField fullWidth label="Current password" type="password" autoComplete="current-password"
            value={current} onChange={(e) => setCurrent(e.target.value)} sx={{ mt: 3 }} autoFocus />
          <TextField fullWidth label="New password" type="password" autoComplete="new-password"
            value={next} onChange={(e) => setNext(e.target.value)} sx={{ mt: 2 }} />
          <TextField fullWidth label="Confirm new password" type="password" autoComplete="new-password"
            value={confirm} onChange={(e) => setConfirm(e.target.value)} sx={{ mt: 2 }}
            error={mismatch} helperText={mismatch ? 'The passwords do not match.' : ' '} />
          <Button fullWidth type="submit" variant="contained" disabled={!ready || busy}
            startIcon={busy ? <CircularProgress size={18} color="inherit" /> : undefined}
            sx={{ mt: 2, py: 1.5, fontWeight: 600, borderRadius: 2 }}>
            Change password
          </Button>
          <Button fullWidth onClick={onSignOut} sx={{ mt: 1, borderRadius: 2 }}>Sign out</Button>
        </Paper>
      </Container>
    </Box>
  );
}
