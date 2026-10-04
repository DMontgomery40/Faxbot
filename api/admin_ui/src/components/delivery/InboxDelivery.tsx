// Email delivery status for received documents, shown in the Inbox: one plain
// line per fax, and a retry when a delivery did not go through.
import { Box, Button, Card, CardContent, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Typography } from '@mui/material';
import type { EmailConnector, IntakeItem } from '../../api/deliveryTypes';
import type { InboundFax } from '../../api/types';
import { formatServerTime, parseServerTime } from '../../api/time';
import { StatusChip, useSmallScreens } from '../access/AccessViews';
import { providerLabel } from '../../providerLabels';

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

// A fax with no delivery record is waiting only while it is new: Faxbot picks
// up a fax within seconds. An older one may be outside the list that was read,
// or have no document to deliver, so nothing is claimed about it.
const PICKUP_WINDOW_MS = 10 * 60 * 1000;

export function isNewFax(receivedAt: string | null | undefined, now = Date.now()): boolean {
  const received = parseServerTime(receivedAt);
  return received !== null && now - received.getTime() < PICKUP_WINDOW_MS;
}

export interface InboundFaxStatus {
  label: string;
  detail: string | null;
  tone: DeliveryTone;
  // The real document is stored and can be downloaded and emailed.
  hasDocument: boolean;
  // Faxbot can be asked to fetch the document again.
  canFetchAgain: boolean;
}

function clockTime(value: string | null | undefined): string | null {
  const date = parseServerTime(value);
  return date ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null;
}

// One chip and at most one sentence per received fax. The retry time is shown
// in the viewer's own time zone rather than the server's.
export function inboundFaxStatus(fax: Pick<InboundFax, 'status' | 'status_text' | 'retry_at' | 'is_test' | 'can_fetch_again'>): InboundFaxStatus {
  const status = (fax.status || '').toLowerCase();
  const sentence = fax.status_text || null;
  // The server says whether there is a source to fetch from; older servers did not.
  const fetchable = fax.can_fetch_again ?? true;
  if (status === 'waiting') {
    const retry = clockTime(fax.retry_at);
    return {
      label: 'Waiting for the document',
      detail: retry ? `The document could not be fetched; Faxbot will try again at ${retry}.` : (sentence ?? 'Waiting for the document.'),
      tone: 'info', hasDocument: false, canFetchAgain: fetchable,
    };
  }
  if (status === 'failed') {
    return { label: 'Not received', detail: sentence, tone: 'error', hasDocument: false, canFetchAgain: fetchable };
  }
  if (fax.is_test) return { label: 'Test fax', detail: null, tone: 'default', hasDocument: true, canFetchAgain: false };
  return { label: 'Received', detail: sentence === 'Received.' ? null : sentence, tone: 'success', hasDocument: true, canFetchAgain: false };
}

// The provider a fax came through, in words people use.
export function providerName(backend: string | null | undefined): string {
  if (!backend) return '-';
  return providerLabel(backend);
}

// Whether an enabled email connector covers this fax number now: one for the
// exact number, or one for every number.
export function emailDeliveryApplies(connectors: EmailConnector[] | null, number: string | null | undefined): boolean {
  if (connectors === null) return true;
  return connectors.some((connector) => connector.enabled && (connector.match_number === null || connector.match_number === number));
}

export function inboxDeliveryStatus(item: IntakeItem | undefined, isNew = true, emailApplies = true): InboxDeliveryStatus | null {
  if (!item) return isNew ? { label: 'Waiting for email delivery', detail: null, tone: 'info', retry: false } : null;
  const reason = GENERIC.has(item.status) ? null : item.status;
  if (item.state === 'delivered') {
    const to = item.delivered_to?.length ? item.delivered_to.join(', ') : null;
    return { label: to ? `Delivered to ${to}` : 'Delivered by email',
      detail: item.delivered_at ? formatServerTime(item.delivered_at) : null, tone: 'success', retry: false };
  }
  if (item.state === 'failed') return { label: 'Not delivered', detail: reason, tone: 'error', retry: true };
  if (item.state === 'sending') return { label: 'Waiting for email delivery', detail: null, tone: 'info', retry: false };
  // Once email delivery is set up for the number, Retry delivery sends it;
  // until then there is nothing to retry.
  if (item.status === NO_EMAIL_DELIVERY && !item.next_attempt_at) {
    return { label: 'No email delivery set up for this number', detail: null, tone: 'default', retry: item.needs_action && emailApplies };
  }
  return { label: 'Waiting for email delivery', detail: reason, tone: item.needs_action ? 'warning' : 'info', retry: item.needs_action };
}

export function DeliveryStatusLine({ item, canRetry, busy, onRetry, label, isNew = true, emailApplies = true, documentPending = false }: {
  item: IntakeItem | undefined;
  canRetry: boolean;
  busy: boolean;
  onRetry: (item: IntakeItem) => void;
  label: string;
  isNew?: boolean;
  emailApplies?: boolean;
  // The fax's document has not arrived, so email delivery waits for it.
  documentPending?: boolean;
}) {
  if (documentPending) return <StatusChip label="Waiting for the document" tone="default" />;
  const status = inboxDeliveryStatus(item, isNew, emailApplies);
  if (!status) return <Typography variant="body2" color="text.secondary">-</Typography>;
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
