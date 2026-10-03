// Organizations that receive documents directly from this Faxbot, and this
// installation's card for partners to add.
import { useState } from 'react';
import {
  Box, Button, Card, CardContent, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  TextField, Typography,
} from '@mui/material';
import HandshakeIcon from '@mui/icons-material/Handshake';
import AdminAPIClient from '../../api/client';
import type { DirectPartner } from '../../api/deliveryTypes';
import { ConfirmDialog, EmptyState, FormDialog, StatusChip, useSmallScreens } from '../access/AccessViews';
import { DeliveryError, Notice } from './shared';

const TONE: Record<DirectPartner['state'], 'success' | 'warning' | 'default'> = {
  verified: 'success', pending: 'warning', revoked: 'default',
};
const LABEL: Record<DirectPartner['state'], string> = { verified: 'Verified', pending: 'Not verified yet', revoked: 'Removed' };

export default function DirectPartners({ client, partners, canWrite, onChanged }: {
  client: AdminAPIClient;
  partners: DirectPartner[];
  canWrite: boolean;
  onChanged: () => void;
}) {
  const { isMobile } = useSmallScreens();
  const [card, setCard] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [pasted, setPasted] = useState('');
  const [confirming, setConfirming] = useState<DirectPartner | null>(null);
  const [code, setCode] = useState('');
  const [removing, setRemoving] = useState<DirectPartner | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const run = async (operation: () => Promise<string | null>) => {
    setBusy(true);
    setError(null);
    try {
      const message = await operation();
      if (message) setNotice(message);
      onChanged();
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const showCard = () => void run(async () => {
    const result = await client.getDirectCard();
    setCard(JSON.stringify(result.card, null, 2));
    return null;
  });

  const add = async () => {
    if (await run(async () => { await client.addDirectPartner(pasted.trim()); return 'Partner added. Send them a code by fax to verify their number.'; })) {
      setAdding(false);
      setPasted('');
    }
  };

  const sendCode = (partner: DirectPartner) => void run(async () => {
    await client.sendDirectCode(partner.id);
    return `A code was faxed to ${partner.fax_number}. ${partner.organization} confirms it from their Faxbot.`;
  });

  const confirm = async () => {
    if (!confirming) return;
    if (await run(async () => (await client.confirmDirectCode(confirming.id, code)).detail)) {
      setConfirming(null);
      setCode('');
    }
  };

  const remove = async () => {
    if (!removing) return;
    if (await run(async () => { await client.removeDirectPartner(removing.id); return null; })) setRemoving(null);
  };

  const actions = (partner: DirectPartner) => canWrite && partner.state !== 'revoked' && (
    <>
      {partner.state === 'pending' && (
        <Button size="small" onClick={() => sendCode(partner)} disabled={busy}>Send code by fax</Button>
      )}
      <Button size="small" onClick={() => { setError(null); setConfirming(partner); }} disabled={busy}>Confirm a code</Button>
      <Button size="small" color="error" onClick={() => { setError(null); setRemoving(partner); }} disabled={busy}>Remove</Button>
    </>
  );

  return (
    <Box>
      <Notice message={notice} onClose={() => setNotice(null)} />
      {!adding && !confirming && !removing && <DeliveryError error={error} onClose={() => setError(null)} />}
      <Box display="flex" gap={1} mb={2} flexWrap="wrap">
        <Button variant="outlined" onClick={showCard} disabled={busy} sx={{ borderRadius: 2 }}>Show our card</Button>
        {canWrite && <Button variant="outlined" onClick={() => { setError(null); setAdding(true); }} sx={{ borderRadius: 2 }}>Add partner</Button>}
      </Box>
      {partners.length === 0 ? (
        <EmptyState icon={<HandshakeIcon />} title="No direct partners"
          text="Exchange cards with an organization you fax often, then verify their number with a code sent by fax." />
      ) : isMobile ? (
        <Stack spacing={2}>
          {partners.map((partner) => (
            <Card key={partner.id} variant="outlined" sx={{ borderRadius: 2 }}>
              <CardContent>
                <Typography variant="subtitle1">{partner.organization}</Typography>
                <Typography variant="body2" color="text.secondary">{partner.fax_number}</Typography>
                <Box my={1}><StatusChip label={LABEL[partner.state]} tone={TONE[partner.state]} /></Box>
                <Typography variant="body2">{partner.status}</Typography>
                <Box mt={1}>{actions(partner)}</Box>
              </CardContent>
            </Card>
          ))}
        </Stack>
      ) : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table>
            <TableHead>
              <TableRow>
                <TableCell>Organization</TableCell>
                <TableCell>Fax number</TableCell>
                <TableCell>Status</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {partners.map((partner) => (
                <TableRow key={partner.id} hover>
                  <TableCell>{partner.organization}</TableCell>
                  <TableCell>{partner.fax_number}</TableCell>
                  <TableCell>
                    <StatusChip label={LABEL[partner.state]} tone={TONE[partner.state]} />
                    <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{partner.status}</Typography>
                  </TableCell>
                  <TableCell align="right">{actions(partner)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <FormDialog open={card !== null} title="Our direct delivery card" submitLabel="Copy" canSubmit
        onSubmit={() => { void navigator.clipboard?.writeText(card ?? ''); setCard(null); setNotice('Card copied.'); }}
        onClose={() => setCard(null)}>
        <Typography variant="body2" sx={{ mb: 1 }}>Send this card to a partner. It contains no secrets.</Typography>
        <TextField fullWidth multiline minRows={8} value={card ?? ''} InputProps={{ readOnly: true, sx: { fontFamily: 'monospace', fontSize: 12 } }} />
      </FormDialog>

      <FormDialog open={adding} title="Add partner" submitLabel="Add partner" busy={busy} error={null}
        canSubmit={pasted.trim().length > 0} onSubmit={() => void add()} onClose={() => setAdding(false)}>
        <DeliveryError error={error} />
        <TextField fullWidth multiline minRows={8} label="Partner's card" value={pasted} onChange={(e) => setPasted(e.target.value)}
          helperText="Paste the card the partner sent you." />
      </FormDialog>

      <FormDialog open={confirming !== null} title={`Confirm a code from ${confirming?.organization ?? ''}`} submitLabel="Confirm"
        busy={busy} error={null} canSubmit={code.replace(/\D/g, '').length === 8} onSubmit={() => void confirm()}
        onClose={() => setConfirming(null)}>
        <DeliveryError error={error} />
        <Typography variant="body2" sx={{ mb: 1 }}>Enter the eight-digit code from the fax {confirming?.organization} sent to your number.</Typography>
        <TextField fullWidth label="Code" value={code} onChange={(e) => setCode(e.target.value)} inputProps={{ inputMode: 'numeric' }} />
      </FormDialog>

      <ConfirmDialog open={removing !== null} title={`Remove ${removing?.organization ?? 'this partner'}?`} danger busy={busy} error={error}
        text="Faxes to this number go by fax again, and documents from this partner are no longer accepted."
        confirmLabel="Remove" onConfirm={() => void remove()} onCancel={() => setRemoving(null)} />
    </Box>
  );
}
