// Pair a phone running the Faxbot app: a six-digit code, shown large with a QR
// code and a countdown. Used from Keys and from the VPN Tunnel settings.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../../api/client';
import { parseServerTime } from '../../api/time';
import QrCode from '../common/QrCode';
import { useSmallScreens } from './AccessViews';

type Pairing = { code?: string; expires_at?: string; error?: string };

export default function PairPhoneDialog({ client, open, onClose }: {
  client: AdminAPIClient;
  open: boolean;
  onClose: () => void;
}) {
  const { isSmallMobile } = useSmallScreens();
  const [pairing, setPairing] = useState<Pairing>({});
  const [remaining, setRemaining] = useState<number | null>(null);

  const createCode = useCallback(async () => {
    setPairing({});
    setRemaining(null);
    try {
      const result = await client.createTunnelPairing();
      setPairing({ code: result.code, expires_at: result.expires_at });
    } catch (error: unknown) {
      setPairing({
        error: error instanceof AdminAPIError && error.status === 403
          ? 'This account is not allowed to pair devices.'
          : 'Could not create a pairing code. Try again.',
      });
    }
  }, [client]);

  useEffect(() => {
    if (open) void createCode();
    else setPairing({});
  }, [open, createCode]);

  useEffect(() => {
    if (!open || !pairing.expires_at) return;
    const expires = parseServerTime(pairing.expires_at)?.getTime() ?? 0;
    const tick = () => setRemaining(Math.max(0, Math.ceil((expires - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [open, pairing.expires_at]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="xs" fullWidth fullScreen={isSmallMobile}>
      <DialogTitle>Pair a phone</DialogTitle>
      <DialogContent>
        {pairing.error ? (
          <Alert severity="error" sx={{ borderRadius: 2 }}>{pairing.error}</Alert>
        ) : !pairing.code ? (
          <Box display="flex" justifyContent="center" py={4}><CircularProgress aria-label="Creating pairing code" /></Box>
        ) : remaining === 0 ? (
          <Alert severity="info" sx={{ borderRadius: 2 }}>This code has expired. Create a new one.</Alert>
        ) : (
          <Stack alignItems="center" spacing={2}>
            <Typography variant="body2" sx={{ textAlign: 'center' }}>
              In the Faxbot app, scan this code or type the number.
            </Typography>
            <Box sx={{ p: 1, bgcolor: '#ffffff', borderRadius: 2, lineHeight: 0 }}>
              <QrCode value={pairing.code} size={208} label={`Pairing code ${pairing.code}`} />
            </Box>
            <Typography variant="h2" component="p" data-testid="pairing-code"
              sx={{ fontFamily: 'monospace', fontWeight: 700, letterSpacing: '0.2em', textAlign: 'center' }}>
              {pairing.code}
            </Typography>
            {remaining !== null && (
              <Typography variant="body2" color="text.secondary">
                Expires in {Math.floor(remaining / 60)}:{String(remaining % 60).padStart(2, '0')}
              </Typography>
            )}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        {(pairing.error || remaining === 0) && <Button onClick={() => void createCode()}>New code</Button>}
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
