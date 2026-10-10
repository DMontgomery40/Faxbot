// The Needs attention block: what waits for a person, grouped by the action it needs, each line opening
// its exact list. Serious problems are marked so the Overview can raise the block while they last.
import { Box, Button, Card, CardContent, Chip, CircularProgress, Typography, useTheme } from '@mui/material';
import { darken } from '@mui/material/styles';
import { ChevronRight as ChevronRightIcon } from '@mui/icons-material';
import type { AdminDestination } from '../../navigation';
import type { AttentionView } from './attention';

function checkedText(at: number): string {
  return `Checked ${new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}.`;
}

export default function NeedsAttention({ view, onNavigate }: {
  view: AttentionView;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const theme = useTheme();
  const warningTextColor = theme.palette.mode === 'light'
    ? darken(theme.palette.warning.light, 0.6)
    : theme.palette.warning.main;
  return (
    <Card sx={{ mb: 3, ...(view.serious ? { borderLeft: 4, borderColor: 'error.main' } : {}) }} data-testid="needs-attention"
      data-serious={String(view.serious)} aria-busy={view.loading} aria-labelledby="needs-attention-title" component="section">
      <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
        <Box display="flex" justifyContent="space-between" alignItems="baseline" gap={2} flexWrap="wrap">
          <Typography variant="h6" component="h2" id="needs-attention-title" sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
            Needs attention
          </Typography>
          {view.checkedAt !== null && (
            <Typography variant="caption" color="text.secondary" data-testid="needs-attention-checked">
              {checkedText(view.checkedAt)}
            </Typography>
          )}
        </Box>
        {view.groups.map((group) => (
          <Box key={group.key} sx={{ mt: 1.5 }}>
            <Typography variant="subtitle2" component="h3" color={group.key === 'serious' ? 'error' : 'text.secondary'}>
              {group.title}
            </Typography>
            <Box display="flex" flexDirection="column" gap={0.5}>
              {group.items.map((item) => (
                <Button key={item.key} data-testid={`attention-${item.key}`} data-serious={String(item.serious)}
                  color="inherit" size="small" disabled={!onNavigate} onClick={() => onNavigate?.(item.destination)}
                  endIcon={<ChevronRightIcon fontSize="small" />}
                  sx={{ justifyContent: 'space-between', textAlign: 'left', textTransform: 'none', px: 1, mx: -1 }}>
                  <Box component="span" sx={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
                    <Typography variant="body2" component="span">
                      {item.label}
                      {item.serious && <Chip component="span" label="Serious" color="error" size="small" sx={{ ml: 1 }} />}
                    </Typography>
                    {item.detail && (
                      <Typography variant="caption" component="span" color="text.secondary">{item.detail}</Typography>
                    )}
                  </Box>
                  {item.count !== null && (
                    <Typography variant="body2" component="span" fontWeight="bold" data-testid="needs-attention-count"
                      color={item.serious ? 'error' : warningTextColor} sx={{ ml: 2 }}>
                      {item.count}
                    </Typography>
                  )}
                </Button>
              ))}
            </Box>
          </Box>
        ))}
        {view.loading && view.items.length === 0 && (
          <CircularProgress size={20} aria-label="Loading Needs attention" sx={{ mt: 1 }} />
        )}
        {view.empty && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{view.empty}</Typography>
        )}
        {view.coverage.map((sentence) => (
          <Typography key={sentence} variant="body2" color="text.secondary" sx={{ mt: 1 }} data-testid="needs-attention-coverage">
            {sentence}
          </Typography>
        ))}
      </CardContent>
    </Card>
  );
}
