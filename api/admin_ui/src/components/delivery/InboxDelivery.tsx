// Email delivery status for received documents, shown in the Inbox: one plain
// line per fax, and a retry when a delivery did not go through.
import { Box, Button, Card, CardContent, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Typography } from '@mui/material';
import type { IntakeItem } from '../../api/deliveryTypes';
import { formatServerTime } from '../../api/time';
import { StatusChip, useSmallScreens } from '../access/AccessViews';

// The server's sentence for a fax whose number has no email delivery.
const NO_EMAIL_DELIVERY = 'No email delivery is set up for this number yet.';
const GENERIC = new Set(['Waiting to be delivered.', 'Being delivered now.', 'Delivered.', 'Not delivered.']);

export type DeliveryTone = 'success' | 'error' | 'warning' | 'info' | 'default';

export interface InboxDeliveryStatus {
  label: string;
  detail: string | null;
  tone: DeliveryTone;
  retry: boolean;
}

export function inboxDeliveryStatus(item: IntakeItem | undefined): InboxDeliveryStatus {
  // Faxbot picks up a new fax within seconds; until then it is simply waiting.
  if (!item) return { label: 'Waiting for email delivery', detail: null, tone: 'info', retry: false };
  const reason = GENERIC.has(item.status) ? null : item.status;
  if (item.state === 'delivered') {
    const to = item.delivered_to?.length ? item.delivered_to.join(', ') : null;
    return { label: to ? `Delivered to ${to}` : 'Delivered by email',
      detail: item.delivered_at ? formatServerTime(item.delivered_at) : null, tone: 'success', retry: false };
  }
  if (item.state === 'failed') return { label: 'Not delivered', detail: reason, tone: 'error', retry: true };
  if (item.state === 'sending') return { label: 'Waiting for email delivery', detail: null, tone: 'info', retry: false };
  if (item.status === NO_EMAIL_DELIVERY && !item.next_attempt_at) {
    return { label: 'No email delivery set up for this number', detail: null, tone: 'default', retry: false };
  }
  return { label: 'Waiting for email delivery', detail: reason, tone: item.needs_action ? 'warning' : 'info', retry: item.needs_action };
}

export function DeliveryStatusLine({ item, canRetry, busy, onRetry, label }: {
  item: IntakeItem | undefined;
  canRetry: boolean;
  busy: boolean;
  onRetry: (item: IntakeItem) => void;
  label: string;
}) {
  const status = inboxDeliveryStatus(item);
  return (
    <Box>
      <StatusChip label={status.label} tone={status.tone} />
      {status.detail && <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{status.detail}</Typography>}
      {item && status.retry && canRetry && (
        <Button size="small" onClick={() => onRetry(item)} disabled={busy} sx={{ mt: 0.5 }} aria-label={`Retry delivery of ${label}`}>
          Retry delivery
        </Button>
      )}
    </Box>
  );
}

function directTitle(item: IntakeItem): string {
  const pages = item.pages ? `, ${item.pages} ${item.pages === 1 ? 'page' : 'pages'}` : '';
  return `From ${item.from_number || 'an unknown number'}${pages}`;
}

// Documents received from partners by direct delivery; they have no fax record.
export function DirectDeliveries({ items, canRetry, busy, onRetry }: {
  items: IntakeItem[];
  canRetry: boolean;
  busy: boolean;
  onRetry: (item: IntakeItem) => void;
}) {
  const { isMobile } = useSmallScreens();
  if (items.length === 0) return null;
  return (
    <Box component="section" sx={{ mt: 4 }}>
      <Typography variant="h6" component="h2">Received by direct delivery</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Documents partners sent straight to this Faxbot, with no fax call.</Typography>
      {isMobile ? (
        <Stack spacing={2}>
          {items.map((item) => (
            <Card key={item.id} sx={{ borderRadius: 2 }}>
              <CardContent>
                <Typography variant="subtitle1">{directTitle(item)}</Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{formatServerTime(item.received_at)}</Typography>
                <DeliveryStatusLine item={item} canRetry={canRetry} busy={busy} onRetry={onRetry} label={directTitle(item)} />
              </CardContent>
            </Card>
          ))}
        </Stack>
      ) : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table>
            <TableHead>
              <TableRow>
                <TableCell>Received</TableCell>
                <TableCell>Document</TableCell>
                <TableCell>To</TableCell>
                <TableCell>Email delivery</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((item) => (
                <TableRow key={item.id} hover>
                  <TableCell>{formatServerTime(item.received_at)}</TableCell>
                  <TableCell>{directTitle(item)}</TableCell>
                  <TableCell>{item.to_number || '-'}</TableCell>
                  <TableCell><DeliveryStatusLine item={item} canRetry={canRetry} busy={busy} onRetry={onRetry} label={directTitle(item)} /></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Box>
  );
}
