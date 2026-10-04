import { useState } from 'react';
import { Alert, Box, Button } from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../api/client';

interface InboundRecoveryProps {
  client: AdminAPIClient;
  // Called after faxes were brought in, so the call list can show them as received.
  onRecovered?: () => void;
}

/**
 * Brings in faxes the SIP trunk received but could not hand to Faxbot. Faxbot
 * also does this by itself every minute; this runs the same check now.
 */
export default function InboundRecovery({ client, onRecovered }: InboundRecoveryProps) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'info' | 'error'; text: string } | null>(null);

  const recover = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await client.recoverInbound();
      setNotice({ severity: result.imported > 0 ? 'success' : 'info', text: result.message });
      if (result.imported > 0) onRecovered?.();
    } catch (error) {
      const detail = error instanceof AdminAPIError ? error.detail : null;
      setNotice({ severity: 'error', text: error instanceof AdminAPIError && error.status === 403
        ? 'You do not have permission to do this.'
        : error instanceof AdminAPIError && error.status === 409 && detail ? detail
          : 'Faxbot could not check for received faxes right now. Try again.' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box sx={{ mt: 1 }}>
      <Button variant="outlined" size="small" onClick={() => { void recover(); }} disabled={busy}>
        Bring in faxes that were received but not handed over
      </Button>
      {notice && <Alert severity={notice.severity} sx={{ mt: 1 }} data-testid="inbound-recovery">{notice.text}</Alert>}
    </Box>
  );
}
