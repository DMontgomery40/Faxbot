// Faxes your rules held: waiting for approval, for a time window, or for a route the rules allow.
// Approve sends a fax at once; Refuse fails it with your reason, and nothing is sent. "Why this route"
// tells, for one fax, which rule chose its route and what happened on each attempt.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Divider, ListItem, ListItemText, Paper, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { formatServerTime } from '../api/time';
import type { AdminDestination } from '../navigation';
import { DeliveryError, formatMoney, Notice } from './delivery/shared';
import type { FaxRoute, Hold, RulesApi } from './ProviderRulesApi';

const KIND_TITLE: Record<Hold['kind'], string> = {
  approval: 'Waiting for approval', window: 'Waiting for its time window', no_route: 'No route your rules allow',
};

function RefuseDialog({ hold, onClose, onRefuse }: { hold: Hold | null; onClose: () => void; onRefuse: (reason: string) => void }) {
  const [reason, setReason] = useState('');
  useEffect(() => { if (hold) setReason(''); }, [hold]);
  return (
    <Dialog open={hold !== null} onClose={onClose} fullWidth maxWidth="sm" aria-labelledby="refuse-title">
      <DialogTitle id="refuse-title">Refuse the fax to {hold?.to_number}?</DialogTitle>
      <DialogContent>
        <Typography variant="body2" sx={{ mb: 2 }}>Nothing is sent. The fax is marked failed with your reason.</Typography>
        <TextField label="Reason" value={reason} onChange={(event) => setReason(event.target.value)} fullWidth autoFocus
          placeholder="Wrong recipient" inputProps={{ maxLength: 500 }} helperText="The sender sees this, and it is kept in the history." />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" color="error" disabled={!reason.trim()} onClick={() => onRefuse(reason.trim())}>Refuse</Button>
      </DialogActions>
    </Dialog>
  );
}

export function HeldFaxes({ api, canApprove, onNavigate, onChanged }: {
  api: RulesApi;
  // Holds the "Approve faxes" permission.
  canApprove: boolean;
  onNavigate?: (destination: AdminDestination) => void;
  onChanged?: () => void;
}) {
  const [holds, setHolds] = useState<Hold[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refusing, setRefusing] = useState<Hold | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.holds().then((value) => setHolds(value.holds)).catch(setError);
  }, [api]);
  useEffect(() => { load(); }, [load]);

  const decide = async (action: () => Promise<Hold>, message: string) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      setNotice(message);
      onChanged?.();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
      load();
    }
  };

  if (holds === null) return error ? <DeliveryError error={error} /> : null;
  if (holds.length === 0 && !notice && !error) return null;
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3, borderRadius: 2 }} aria-label="Faxes waiting for you">
      <Typography variant="h6" component="h2">Faxes waiting for you</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Your rules held these faxes. Nothing has been sent for them.
      </Typography>
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      {holds.length > 0 && (
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small" aria-label="Held faxes">
            <TableHead>
              <TableRow><TableCell>To</TableCell><TableCell>Pages</TableCell><TableCell>Sent by</TableCell>
                <TableCell>Waiting since</TableCell><TableCell>Why</TableCell><TableCell /></TableRow>
            </TableHead>
            <TableBody>
              {holds.map((hold) => (
                <TableRow key={hold.id}>
                  <TableCell>{hold.to_number}</TableCell>
                  <TableCell>{hold.pages ?? '-'}</TableCell>
                  <TableCell>{hold.sender_name ?? '-'}</TableCell>
                  <TableCell>{formatServerTime(hold.requested_at)}</TableCell>
                  <TableCell>
                    <Typography variant="body2">{hold.reason}</Typography>
                    {hold.kind === 'window' && hold.until && (
                      <Typography variant="caption" color="text.secondary">Goes out at {formatServerTime(hold.until)}.</Typography>
                    )}
                    {canApprove && !hold.can_decide && (
                      <Typography variant="caption" color="text.secondary" display="block">
                        Someone other than the sender must decide on this fax.
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                    {canApprove && hold.can_decide && hold.kind !== 'window' && (
                      <Button size="small" variant="contained" disabled={busy} aria-label={`Approve the fax to ${hold.to_number}`}
                        onClick={() => void decide(() => api.approve(hold), `Approved. The fax to ${hold.to_number} goes out now.`)}>
                        Approve
                      </Button>
                    )}
                    {canApprove && hold.can_decide && (
                      <Button size="small" color="error" disabled={busy} aria-label={`Refuse the fax to ${hold.to_number}`}
                        onClick={() => setRefusing(hold)}>Refuse</Button>
                    )}
                    {hold.kind === 'no_route' && onNavigate && (
                      <Button size="small" onClick={() => onNavigate('providers/rules')}>Edit rules</Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      )}
      {!canApprove && holds.some((hold) => hold.kind === 'approval') && (
        <Alert severity="info" sx={{ mt: 1 }}>
          Approving a fax needs the Approve faxes permission. Give it on Access → Roles.
        </Alert>
      )}
      <RefuseDialog hold={refusing} onClose={() => setRefusing(null)} onRefuse={(reason) => {
        const hold = refusing!;
        setRefusing(null);
        void decide(() => api.refuse(hold, reason), `Refused. Nothing was sent to ${hold.to_number}.`);
      }} />
    </Paper>
  );
}

// "Why this route" for one sent fax, as items for the fax's details.
export function FaxRouteItems({ api, jobId }: { api: RulesApi; jobId: string }) {
  const [route, setRoute] = useState<FaxRoute | null>(null);
  useEffect(() => {
    let live = true;
    setRoute(null);
    api.faxRoute(jobId).then((value) => { if (live) setRoute(value); }).catch(() => { if (live) setRoute(null); });
    return () => { live = false; };
  }, [api, jobId]);
  if (!route?.sentence) return null;
  return (
    <>
      <Divider />
      <ListItem><ListItemText primary="Why this route" secondary={route.sentence} /></ListItem>
      {route.attempts.map((attempt) => (
        <ListItem key={attempt.number} sx={{ pl: 4 }}>
          <ListItemText primary={`Attempt ${attempt.number}: ${attempt.account_label}`} secondary={(
            <Stack component="span" spacing={0.25}>
              <span>{attempt.sentence}</span>
              {attempt.dialed_number && <span>Number dialed: {attempt.dialed_number}</span>}
              {attempt.page_layout && <span>Pages per sheet: {attempt.page_layout}</span>}
              {attempt.estimate && <span>About {formatMoney(attempt.estimate)} (estimate)</span>}
            </Stack>
          )} />
        </ListItem>
      ))}
      {route.hold && <ListItem><ListItemText primary={KIND_TITLE[route.hold.kind]} secondary={route.hold.reason} /></ListItem>}
    </>
  );
}
