// Providers → In use: what sends this installation's faxes, what receives them and
// any further sending routes, each opening its own page. Choosing or changing a
// provider happens in the Setup wizard, not from a list of every provider.
import { Box, Button, Paper, Stack, Typography } from '@mui/material';
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline';
import type { ConsoleContext } from '../api/types';
import type { AdminDestination } from '../navigation';
import { providerLabel } from '../providerLabels';

const PAGE: Record<string, string> = { sip: 'trunk' };

export function providerPage(provider: string): AdminDestination {
  return `providers/${PAGE[provider] ?? provider}`;
}

export default function ProvidersInUse({ context, canChange, onNavigate }: {
  context: ConsoleContext;
  // May this person change providers (the Setup wizard)?
  canChange: boolean;
  onNavigate: (destination: AdminDestination) => void;
}) {
  const view = context.provider_view;
  const rows: Array<[string, string[]]> = view ? [
    ['Sending', view.active_outbound ? [view.active_outbound] : []],
    ['Receiving', view.active_inbound ? [view.active_inbound] : []],
    ['Further sending routes', (view.extra_routes ?? []).filter((route) => route !== 'direct')],
  ] : [];
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, mb: 3 }} data-testid="providers-in-use">
      {view ? (
        <Stack spacing={1}>
          {rows.filter(([, providers], index) => index < 2 || providers.length > 0).map(([title, providers]) => (
            <Box key={title} display="flex" gap={1} alignItems="center" flexWrap="wrap">
              <Typography variant="body2" sx={{ minWidth: 180 }} color="text.secondary">{title}</Typography>
              {providers.length === 0 ? <Typography variant="body2">No provider</Typography>
                : providers.map((provider) => (
                  <Button key={provider} size="small" sx={{ textTransform: 'none' }} onClick={() => onNavigate(providerPage(provider))}>
                    {providerLabel(provider)}
                  </Button>
                ))}
            </Box>
          ))}
        </Stack>
      ) : (
        <Typography variant="body2" color="text.secondary">The providers in use are shown to people who may read provider settings.</Typography>
      )}
      {canChange && (
        <Button variant="outlined" startIcon={<AddCircleOutlineIcon />} sx={{ mt: 2, borderRadius: 2 }}
          onClick={() => onNavigate('system/setup')}>
          Add or change a provider
        </Button>
      )}
    </Paper>
  );
}
