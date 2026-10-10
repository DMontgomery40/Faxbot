// Savings & optimization → Prices & plans: what each sending route publishes about calling toll-free numbers, used when a
// recipient approved a toll-free number for its faxes. Read-only: these are the providers' published terms.
import { Box, Link, Stack, Typography } from '@mui/material';
import type { TollFreeTerms } from '../../api/deliveryTypes';
import { formatLocalDate } from '../../api/time';

export default function TollFreePrices({ items }: { items: TollFreeTerms[] }) {
  if (!items.length) return null;
  return (
    <Box mt={3} data-testid="toll-free-prices">
      <Typography variant="subtitle1" component="h3">Calls to toll-free numbers</Typography>
      <Typography variant="body2" color="text.secondary" mb={1}>
        When a recipient approves a toll-free number, Faxbot calls it on a route that can, and the recipient pays for the call.
      </Typography>
      <Stack spacing={1.5}>
        {items.map((item) => (
          <Box key={item.provider_id} data-testid={`toll-free-${item.provider_id}`}>
            <Typography variant="body2" fontWeight={600}>{item.provider_name}</Typography>
            <Typography variant="body2">{item.reach_text}</Typography>
            <Typography variant="body2">Price: {item.price_text}</Typography>
            {item.caller_id_text && <Typography variant="body2">Caller ID it needs: {item.caller_id_text}</Typography>}
            {item.advertised_on && (
              <Typography variant="caption" color="text.secondary">
                Advertised on {formatLocalDate(item.advertised_on)}
                {item.source_url ? <> · <Link href={item.source_url} target="_blank" rel="noreferrer">source</Link></> : null}
              </Typography>
            )}
          </Box>
        ))}
      </Stack>
    </Box>
  );
}
