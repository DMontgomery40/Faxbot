// Fax numbers Faxbot has sent to: which routes worked, what they cost, and the
// route order for the next fax. Table on desktop, cards on mobile.
import { useEffect, useState } from 'react';
import {
  Box, Button, Card, CardContent, Chip, FormControlLabel, List, ListItem, ListItemText, Paper, Stack, Switch,
  Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import PhoneIcon from '@mui/icons-material/Phone';
import AdminAPIClient from '../../api/client';
import type { Destination, DestinationDetail, DirectPartner } from '../../api/deliveryTypes';
import { EmptyState, Field, FormDialog, useSmallScreens } from '../access/AccessViews';
import { DeliveryError, formatMoneyList, formatPercent } from './shared';
import { numberPlaceholder, useNumberFormat } from '../common/numbers';
import { SendingTogetherPanel } from './SendingTogether';
import RecipientFaxLimitsPanel from './RecipientFaxLimits';

function routeSummary(destination: Destination): string {
  if (destination.routes.length === 0) return 'No faxes sent yet';
  return destination.routes.map((route) => `${route.label}: ${formatPercent(route.success_percent)}`).join(' · ');
}

export function DestinationDialog({ client, number, canWrite, onClose, onSaved }: {
  client: AdminAPIClient;
  number: string | null;
  canWrite: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [detail, setDetail] = useState<DestinationDetail | null>(null);
  const [name, setName] = useState('');
  const [notes, setNotes] = useState('');
  const [preferred, setPreferred] = useState('');
  const [references, setReferences] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!number) return;
    let current = true;
    setDetail(null);
    setError(null);
    client.getDestination(number).then((loaded) => {
      if (!current) return;
      setDetail(loaded);
      setName(loaded.display_name ?? '');
      setNotes(loaded.notes ?? '');
      setPreferred(loaded.preferred_route ?? '');
      setReferences(loaded.accepts_references);
    }).catch((failure) => current && setError(failure));
    return () => { current = false; };
  }, [client, number]);

  const save = async () => {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      await client.updateDestination(detail.number, {
        display_name: name.trim() || null, notes: notes.trim() || null, preferred_route: preferred || null,
        accepts_references: references, version: detail.version,
      });
      onSaved();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const options = [
    ...(detail?.direct_partner ? [{ value: 'direct', label: 'Direct delivery' }] : []),
    ...(detail?.available_routes ?? []).map((route) => ({ value: route.route, label: route.label })),
  ];

  return (
    <FormDialog open={number !== null} title={detail?.display_name || detail?.number || number || ''} submitLabel="Save" busy={busy}
      error={null} canSubmit={canWrite && detail !== null} onSubmit={() => void save()} onClose={onClose}>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {detail && (
        <>
          <Typography variant="body2" color="text.secondary" gutterBottom>{detail.number}</Typography>
          {detail.direct_partner && (
            <Typography sx={{ mb: 1 }}>Faxes to this number go straight to {detail.direct_partner.organization}, with no fax call.</Typography>
          )}
          <Typography variant="subtitle2" sx={{ mt: 1 }}>Route order for the next fax</Typography>
          <List dense>
            {detail.recommended_routes.map((route, index) => (
              <ListItem key={route.route} disableGutters>
                <ListItemText
                  primary={`${index + 1}. ${route.label}${route.included_in_plan ? ' · included in your plan'
                    : route.rate ? ` · about ${route.rate}` : ' · cost unknown'}`}
                  secondary={route.explanation} />
              </ListItem>
            ))}
          </List>
          {detail.routes.length > 0 && (
            <>
              <Typography variant="subtitle2">Last 30 days</Typography>
              {detail.routes.map((route) => (
                <Typography key={route.route} variant="body2" color="text.secondary">
                  {route.label}: {route.attempts} {route.attempts === 1 ? 'fax' : 'faxes'}, {formatPercent(route.success_percent)}, {formatMoneyList(route.estimated_cost_30_days, 'no cost recorded')}
                </Typography>
              ))}
            </>
          )}
          <Field label="Name" value={name} onChange={setName} disabled={!canWrite} />
          <TextField select fullWidth margin="normal" label="Preferred route" value={preferred}
            onChange={(e) => setPreferred(e.target.value)} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}
            helperText={preferred ? 'Faxbot tries this route first for this number.' : 'Faxbot picks the cheapest route that works reliably.'}
            disabled={!canWrite}>
            <option value="">Cheapest reliable route</option>
            {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </TextField>
          <TextField fullWidth margin="normal" label="Notes" value={notes} onChange={(e) => setNotes(e.target.value)}
            multiline minRows={2} disabled={!canWrite} />
          <FormControlLabel sx={{ mt: 1 }} control={<Switch checked={references} onChange={(e) => setReferences(e.target.checked)} disabled={!canWrite} />}
            label="Accepts a one-page index instead of documents it already received for a case" />
          <SendingTogetherPanel client={client} number={detail.number} canWrite={canWrite} />
          <RecipientFaxLimitsPanel client={client} number={detail.number} canWrite={canWrite} />
        </>
      )}
    </FormDialog>
  );
}

// The route a number is sent by first: the preferred one, or Faxbot's own choice.
function preferredText(destination: Destination): string {
  if (!destination.preferred_route) return 'Cheapest reliable';
  if (destination.preferred_route === 'direct') return 'Direct delivery';
  return destination.routes.find((route) => route.route === destination.preferred_route)?.label ?? destination.preferred_route;
}

function partnerText(destination: Destination, partners: DirectPartner[] | null): string {
  const partner = (partners ?? []).find((peer) => peer.fax_number === destination.number && peer.state !== 'revoked');
  if (!partner) return '-';
  return partner.state === 'verified' ? partner.organization : `${partner.organization} (waiting for verification)`;
}

export default function Destinations({ client, destinations, canWrite, onChanged, partners = null }: {
  client: AdminAPIClient;
  destinations: Destination[];
  canWrite: boolean;
  onChanged: () => void;
  // Direct delivery partners, to say which numbers belong to one; null when not readable.
  partners?: DirectPartner[] | null;
}) {
  const { isMobile } = useSmallScreens();
  const [open, setOpen] = useState<string | null>(null);
  const [lookup, setLookup] = useState('');
  const numberFormat = useNumberFormat(client);

  const saved = () => { setOpen(null); onChanged(); };

  return (
    <Box>
      <Box display="flex" gap={1} alignItems="center" mb={2} flexWrap="wrap">
        <TextField size="small" label="Fax number" value={lookup} onChange={(e) => setLookup(e.target.value)}
          type="tel" placeholder={numberPlaceholder(numberFormat)}
          sx={{ minWidth: 220 }} />
        <Button variant="outlined" disabled={!lookup.trim()} onClick={() => setOpen(lookup.trim())} sx={{ borderRadius: 2 }}>
          Look up
        </Button>
      </Box>
      {destinations.length === 0 ? (
        <EmptyState icon={<PhoneIcon />} title="No fax numbers yet" text="Numbers appear here after Faxbot sends to them, or when you look one up." />
      ) : isMobile ? (
        <Stack spacing={2}>
          {destinations.map((destination) => (
            <Card key={destination.number} variant="outlined" sx={{ borderRadius: 2 }}>
              <CardContent>
                <Typography variant="subtitle1">{destination.display_name || destination.number}</Typography>
                {destination.display_name && <Typography variant="body2" color="text.secondary">{destination.number}</Typography>}
                <Typography variant="body2" sx={{ mt: 1 }}>{routeSummary(destination)}</Typography>
                <Typography variant="body2" color="text.secondary">Last 30 days: {formatMoneyList(destination.estimated_cost_30_days)}</Typography>
                <Box mt={1} display="flex" gap={1} alignItems="center">
                  {destination.preferred_route && <Chip size="small" label="Preferred route set" />}
                  <Button size="small" onClick={() => setOpen(destination.number)}>Details</Button>
                </Box>
              </CardContent>
            </Card>
          ))}
        </Stack>
      ) : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table>
            <TableHead>
              <TableRow>
                <TableCell>Fax number</TableCell>
                <TableCell>Routes</TableCell>
                <TableCell>Last 30 days</TableCell>
                <TableCell>Preferred way to send</TableCell>
                <TableCell>Partner</TableCell>
                <TableCell>Case packets</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {destinations.map((destination) => (
                <TableRow key={destination.number} hover>
                  <TableCell>
                    <Typography variant="body2">{destination.display_name || destination.number}</Typography>
                    {destination.display_name && <Typography variant="caption" color="text.secondary">{destination.number}</Typography>}
                  </TableCell>
                  <TableCell>{routeSummary(destination)}</TableCell>
                  <TableCell>{formatMoneyList(destination.estimated_cost_30_days)}</TableCell>
                  <TableCell>{preferredText(destination)}</TableCell>
                  <TableCell>{partnerText(destination, partners)}</TableCell>
                  <TableCell>{destination.accepts_references ? 'Takes a one-page list instead' : 'Full documents'}</TableCell>
                  <TableCell align="right">
                    <Button size="small" onClick={() => setOpen(destination.number)} aria-label={`Details for ${destination.number}`}>Details</Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      <DestinationDialog client={client} number={open} canWrite={canWrite} onClose={() => setOpen(null)} onSaved={saved} />
    </Box>
  );
}
