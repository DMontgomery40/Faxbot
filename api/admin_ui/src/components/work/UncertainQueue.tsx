// Sent faxes to settle, beside the received work: one row each, opened in Sent with its checks.
import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Chip, List, ListItem, ListItemText, Stack, ToggleButton, ToggleButtonGroup,
  Typography,
} from '@mui/material';
import type AdminAPIClient from '../../api/client';
import type { CertaintyCounts, CertaintyItem } from '../../api/certaintyTypes';
import { formatServerTime } from '../../api/time';
import { deliveryErrorMessage } from '../delivery/shared';
import { certaintyStateSentence } from './SentCertainty';

type View = 'all' | 'mine' | 'overdue';

export function uncertainSummary(counts: CertaintyCounts): string {
  const open = counts.open === 1 ? '1 sent fax needs settling' : `${counts.open} sent faxes need settling`;
  return counts.overdue ? `${open}; ${counts.overdue} overdue.` : `${open}.`;
}

export default function UncertainQueue({ client, onOpenSentFax }: {
  client: AdminAPIClient;
  onOpenSentFax?: (faxId: string) => void;
}) {
  const [counts, setCounts] = useState<CertaintyCounts | null>(null);
  const [items, setItems] = useState<CertaintyItem[] | null>(null);
  const [view, setView] = useState<View>('all');
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let current = true;
    Promise.all([client.uncertainCounts(), client.listUncertain({ view, limit: 20 })]).then(([nextCounts, next]) => {
      if (!current) return;
      setCounts(nextCounts);
      setItems(next.items);
    }).catch((failure) => { if (current) setError(failure); });
    return () => { current = false; };
  }, [client, view]);

  if (error) return <Alert severity="error" sx={{ mb: 2 }}>{deliveryErrorMessage(error)}</Alert>;
  if (!counts || !items || counts.open === 0) return null;
  return (
    <Card variant="outlined" sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} justifyContent="space-between" alignItems={{ sm: 'center' }}>
          <Box>
            <Typography variant="h6" component="h2">Sent faxes to settle</Typography>
            <Typography variant="body2" color="text.secondary">
              {uncertainSummary(counts)} Faxbot could not confirm these arrived, and never sends them again on its own.
            </Typography>
          </Box>
          <ToggleButtonGroup size="small" exclusive value={view} onChange={(_, next) => { if (next) setView(next); }}
            aria-label="Which sent faxes">
            <ToggleButton value="all">All</ToggleButton>
            <ToggleButton value="mine">Mine ({counts.mine})</ToggleButton>
            <ToggleButton value="overdue">Overdue ({counts.overdue})</ToggleButton>
          </ToggleButtonGroup>
        </Stack>
        <List dense>
          {items.map((item) => (
            <ListItem key={item.id} disableGutters secondaryAction={onOpenSentFax
              ? <Button size="small" onClick={() => onOpenSentFax(item.fax_id)}>Open</Button> : undefined}>
              <ListItemText
                primary={<Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                  <span>To {item.to_number}, sent {formatServerTime(item.sent_at)}</span>
                  {item.overdue && <Chip size="small" color="error" label="Overdue" />}
                </Stack>}
                secondary={`${item.why} ${certaintyStateSentence(item)}`} />
            </ListItem>
          ))}
          {items.length === 0 && <ListItem disableGutters><ListItemText secondary="None in this view." /></ListItem>}
        </List>
      </CardContent>
    </Card>
  );
}
