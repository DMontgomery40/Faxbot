import { useState } from 'react';
import { Alert, Box, Button, CircularProgress, IconButton, Tooltip, Typography } from '@mui/material';
import { ContentCopy as ContentCopyIcon } from '@mui/icons-material';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../api/client';

// The Telnyx Fax API's own page (Delivery setup → Telnyx Fax API): what it costs and what Telnyx needs first.
export const TELNYX_FAX_PRICE_SENTENCE = "Telnyx charges $0.007 a page plus the call's minutes. Before Faxbot "
  + 'sends, assign an outbound voice profile to the Fax Application in your Telnyx account.';
export const TELNYX_FAX_KEY_HELP = 'From your Telnyx account under Keys & Credentials. A Telnyx trunk uses the same '
  + 'key, so changing it here changes it there too.';
export const TELNYX_FAX_APPLICATION_HELP = 'Leave it empty and choose Create a Fax Application below, or paste the ID '
  + 'of one you made in Telnyx.';
export const TELNYX_FAX_NUMBER_HELP = 'A number in your Telnyx account, such as +13035550100. Faxes come from it.';
export const TELNYX_FAX_PUBLIC_KEY_HELP = "From your Telnyx account under Keys & Credentials, Public Key. With it, "
  + "Faxbot hears each fax's result at once; without it, Faxbot checks with Telnyx until the fax is done.";
export const TELNYX_FAX_RESULTS_HELP = 'Faxbot gives this address to the Fax Application it creates. For one you made '
  + "yourself, paste it into the application's webhook URL in Telnyx.";
export const TELNYX_FAX_CREATE_HELP = 'Faxbot creates a Fax Application in your Telnyx account with the saved API key '
  + 'and fills in its ID here.';
export const TELNYX_FAX_SAVE_KEY_FIRST = 'Save the Telnyx API key first; Faxbot then can create the Fax Application.';
export const TELNYX_FAX_NO_PERMISSION = 'You do not have permission to change provider settings.';
const FAILED = 'Faxbot could not create the Fax Application. Try again.';

interface TelnyxFaxApplicationProps {
  client: AdminAPIClient;
  // The saved Telnyx API key (masked) and Fax Application ID.
  apiKeySaved: boolean;
  connectionId: string;
  resultsUrl?: string;
  // May this person change settings now (permission, and the settings are loaded and current).
  canWrite: boolean;
  // Reload the settings after Faxbot saved the new Fax Application's ID.
  onCreated: () => void | Promise<void>;
}

export function TelnyxFaxApplication({ client, apiKeySaved, connectionId, resultsUrl, canWrite, onCreated }:
  TelnyxFaxApplicationProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const create = async () => {
    if (busy || !canWrite) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const accounts = await client.getProviderAccounts();
      const result = await client.createTelnyxFaxApplication('telnyx_fax', accounts.generation);
      setDone(result.created?.sentence || 'Faxbot created the Fax Application and saved its ID.');
      await onCreated();
    } catch (problem) {
      if (isForbidden(problem)) setError(TELNYX_FAX_NO_PERMISSION);
      else if (problem instanceof AdminAPIError && problem.detail?.trim()) setError(problem.detail.trim());
      else setError(FAILED);
    } finally {
      setBusy(false);
    }
  };

  const copy = () => {
    if (resultsUrl) void navigator.clipboard?.writeText(resultsUrl).catch(() => undefined);
  };

  return (
    <Box sx={{ mt: 2 }} data-testid="telnyx-fax-application">
      {resultsUrl && (
        <Alert severity="info" sx={{ mb: 2 }} data-testid="telnyx-fax-results-url">
          <Typography variant="body2" sx={{ fontWeight: 600 }}>Address for fax results</Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>{resultsUrl}</Typography>
            <Tooltip title="Copy the address">
              <IconButton size="small" aria-label="Copy the address for fax results" onClick={copy}>
                <ContentCopyIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Box>
          <Typography variant="body2" sx={{ mt: 1 }}>{TELNYX_FAX_RESULTS_HELP}</Typography>
        </Alert>
      )}
      {!connectionId && (
        <Box>
          <Button variant="outlined" onClick={create} disabled={busy || !canWrite || !apiKeySaved}
            startIcon={busy ? <CircularProgress size={16} /> : undefined}>
            {busy ? 'Creating the Fax Application…' : 'Create a Fax Application'}
          </Button>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
            {!canWrite ? TELNYX_FAX_NO_PERMISSION : apiKeySaved ? TELNYX_FAX_CREATE_HELP : TELNYX_FAX_SAVE_KEY_FIRST}
          </Typography>
        </Box>
      )}
      {done && <Alert severity="success" sx={{ mt: 2 }} role="status">{done}</Alert>}
      {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
    </Box>
  );
}
