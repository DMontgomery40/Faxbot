// One work item in a side panel: its details and its history, one sentence per event.
import { useEffect, useState } from 'react';
import { Alert, Box, Button, Divider, Drawer, List, ListItem, ListItemText, Stack, Typography } from '@mui/material';
import DownloadIcon from '@mui/icons-material/Download';
import type AdminAPIClient from '../../api/client';
import { formatServerTime } from '../../api/time';
import type { InboundFax, WorkEvent, WorkItem } from '../../api/types';
import type { IntakeItem } from '../../api/deliveryTypes';
import { RECIPIENTS_NOT_RECORDED, earlierFailuresText } from '../delivery/InboxDelivery';
import { deliveryErrorMessage } from '../delivery/shared';
import { ReceivedCallNegotiation } from '../CallNegotiation';
import { ReceivedEncodedPages } from '../delivery/EncodedPages';
import { can, duplicateSentence, maskNumber, workStateSentence } from './text';

function Field({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null;
  return (
    <Box sx={{ mb: 1.5 }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="body2">{value}</Typography>
    </Box>
  );
}

// Who a delivered email went to, as stored when the email server accepted it; never the connector's addresses today.
function emailedTo(delivery: IntakeItem | null): string | null {
  if (!delivery || delivery.state !== 'delivered') return null;
  if (delivery.delivered_to?.length) return delivery.delivered_to.join(', ');
  return delivery.recipients_recorded === false ? RECIPIENTS_NOT_RECORDED : null;
}

export default function WorkDetail({ client, item, onClose, onDownload, fax = null, delivery = null }: {
  client: AdminAPIClient;
  item: WorkItem | null;
  onClose: () => void;
  onDownload: (item: WorkItem) => void;
  // The received fax behind this item, when this person may list it.
  fax?: InboundFax | null;
  // Its email delivery, when this person may read deliveries.
  delivery?: IntakeItem | null;
}) {
  const [events, setEvents] = useState<WorkEvent[] | null>(null);
  const [detail, setDetail] = useState<WorkItem | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!item) return;
    let current = true;
    setEvents(null);
    setDetail(null);
    setError(null);
    Promise.all([client.getWork(item.id), client.workHistory(item.id)]).then(([next, history]) => {
      if (!current) return;
      setDetail(next);
      setEvents(history.events);
    }).catch((failure) => {
      if (current) setError(failure);
    });
    return () => { current = false; };
  }, [client, item]);

  const shown = detail ?? item;
  return (
    <Drawer anchor="right" open={Boolean(item)} onClose={onClose}>
      <Box sx={{ width: { xs: '100vw', sm: 420 }, p: 3 }} role="dialog" aria-label="Work item">
        {shown && (
          <>
            <Typography variant="h6" sx={{ mb: 1 }}>{workStateSentence(shown)}</Typography>
            {shown.owner_can_see === false && (
              <Alert severity="warning" sx={{ mb: 2 }}>
                {shown.owner?.name || 'The owner'} can no longer see this document; assign someone else.
              </Alert>
            )}
            <Field label="From" value={maskNumber(shown.from_number)} />
            <Field label="Mailbox" value={shown.mailbox ?? 'Not in a mailbox'} />
            <Field label="Arrived" value={formatServerTime(shown.available_at)} />
            <ReceivedCallNegotiation client={client} faxId={shown.inbound_fax_id} />
            <ReceivedEncodedPages client={client} faxId={shown.inbound_fax_id} canDownload={can(shown, 'document')} />
            <Field label="Target" value={shown.due_text} />
            <Field label="Due" value={shown.due_at ? formatServerTime(shown.due_at) : null} />
            <Field label="Owner" value={shown.owner?.name} />
            <Field label="Same document" value={duplicateSentence(shown)} />
            <Field label="Earlier failures" value={fax ? earlierFailuresText(fax) : null} />
            <Field label="Emailed to" value={emailedTo(delivery)} />
            <Field label="Done note" value={shown.done_note} />
            {can(shown, 'document') && (
              <Button startIcon={<DownloadIcon />} onClick={() => onDownload(shown)} sx={{ mb: 2 }}>Download document</Button>
            )}
            <Divider sx={{ my: 2 }} />
            <Typography variant="subtitle2">History</Typography>
            {error ? <Alert severity="error" sx={{ mt: 1 }}>{deliveryErrorMessage(error)}</Alert> : null}
            {events === null && !error ? <Typography variant="body2" color="text.secondary">Loading…</Typography> : null}
            <List dense>
              {(events ?? []).map((event, index) => (
                <ListItem key={`${event.occurred_at}-${index}`} disableGutters>
                  <ListItemText primary={event.text} secondary={formatServerTime(event.occurred_at)} />
                </ListItem>
              ))}
            </List>
            <Stack direction="row" justifyContent="flex-end"><Button onClick={onClose}>Close</Button></Stack>
          </>
        )}
      </Box>
    </Drawer>
  );
}
