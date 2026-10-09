// Sent details for a fax whose call broke part way: send only the pages the receiving machine did not confirm,
// and the link both ways between a fax and the new fax that carries its remaining pages.
// Nothing is sent by itself: the pages go only when the person selects the button.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Divider, Stack, TextField, Typography } from '@mui/material';
import type AdminAPIClient from '../../api/client';
import type { ContinuationView } from '../../api/continuationTypes';
import { deliveryErrorMessage } from '../delivery/shared';

const capitalized = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

// "Pages 8–20 are on their way as a new fax." ("Page 20 is ..." for one page).
export function onTheirWay(pagesText: string): string {
  return `${capitalized(pagesText)} ${pagesText.startsWith('page ') ? 'is' : 'are'} on ${pagesText.startsWith('page ') ? 'its' : 'their'} way as a new fax.`;
}

// Why these pages, what they cost against the whole fax, and that the first may arrive twice.
export function ContinuationFacts({ basis, costText, warning }: { basis?: string | null; costText?: string; warning?: string }) {
  return (
    <Stack spacing={0.5} sx={{ my: 1 }}>
      {basis && <Typography variant="body2">{basis}</Typography>}
      {costText && <Typography variant="body2">{costText}</Typography>}
      {warning && <Typography variant="body2" color="text.secondary">{warning}</Typography>}
    </Stack>
  );
}

export function SentContinuation({ client, jobId, onOpenFax }: {
  client: AdminAPIClient; jobId: string; onOpenFax?: (faxId: string) => void;
}) {
  const [view, setView] = useState<ContinuationView | null>(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => {
    let current = true;
    client.continuationForFax(jobId).then((next) => { if (current) setView(next); })
      .catch(() => { if (current) setView(null); });
    return () => { current = false; };
  }, [client, jobId]);
  useEffect(() => load(), [load]);

  if (!view) return null;
  const offer = view.offer;
  // With an uncertain item waiting to be settled, the action sits on that item, beside "send it again".
  const showOffer = offer !== null && !offer.open_item_id;
  const sent = view.continued_by && !view.continued_by.from_item ? view.continued_by : null;
  if (!view.continues && !sent && !showOffer) return null;

  const send = async () => {
    if (!offer || offer.first_page === undefined) return;
    setBusy(true);
    setError(null);
    try {
      const next = await client.sendContinuation(jobId, { first_page: offer.first_page, reason: reason.trim() || undefined });
      setView(next);
      setNotice(onTheirWay(offer.pages_text ?? 'the remaining pages'));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const open = (faxId: string) => (onOpenFax ? <Button onClick={() => onOpenFax(faxId)}>Open it</Button> : undefined);

  return (
    <>
      <Divider />
      <Box sx={{ px: 2, py: 2 }}>
        <Typography variant="h6" component="h2" gutterBottom>Remaining pages</Typography>
        {view.continues && (
          <Alert severity="info" sx={{ mb: 1 }} action={open(view.continues.fax_id)}>
            {`This fax carries ${view.continues.pages_text} of an earlier fax whose call broke.`}
          </Alert>
        )}
        {notice && <Alert severity="success" sx={{ mb: 1 }} onClose={() => setNotice(null)}>{notice}</Alert>}
        {sent && (
          <Alert severity="info" sx={{ mb: 1 }} action={open(sent.fax_id)}>
            {`${capitalized(sent.pages_text)} went as a new fax.`}
          </Alert>
        )}
        {error ? <Alert severity="error" sx={{ mb: 1 }} onClose={() => setError(null)}>{deliveryErrorMessage(error)}</Alert> : null}
        {showOffer && offer && !offer.available && <Typography variant="body2">{offer.reason}</Typography>}
        {showOffer && offer && offer.available && (
          <>
            <Typography variant="body2">
              The call broke part way. You can send only the pages the receiving machine did not confirm, as a new fax linked to this one.
            </Typography>
            <ContinuationFacts basis={offer.basis} costText={offer.cost_text} warning={offer.warning} />
            {offer.may_send ? (
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} alignItems={{ sm: 'center' }}>
                <TextField size="small" label="Reason (optional)" value={reason} disabled={busy}
                  onChange={(event) => setReason(event.target.value)} inputProps={{ maxLength: 400 }} />
                <Button variant="contained" onClick={() => void send()} disabled={busy}>{offer.action}</Button>
              </Stack>
            ) : (
              <Typography variant="body2" color="text.secondary">
                Only the person who sent this fax, or someone who may confirm receipt of it, can send these pages.
              </Typography>
            )}
          </>
        )}
      </Box>
    </>
  );
}

export default SentContinuation;
