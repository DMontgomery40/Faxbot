// The Overview's value blocks and everyday counts, each with its own state and time (blocks.ts).
import type { ReactNode } from 'react';
import { Box, Button, Card, CardContent, Chip, CircularProgress, Link, Typography } from '@mui/material';
import { ChevronRight as ChevronRightIcon, Send as SendIcon } from '@mui/icons-material';
import type { AdminDestination } from '../../navigation';
import {
  BLOCK_TEXT, clockText, staleText, type BlockState, type ConnectNext, type EverydayLine, type Improvement,
  type ResultLine, type UsedCapability,
} from './blocks';

// Addresses of the new console areas (Savings & optimization) are passed on as they are; the shell resolves them.
export type Navigate = (destination: AdminDestination) => void;
export const go = (onNavigate: Navigate | undefined, destination: string) => onNavigate?.(destination as AdminDestination);

export function BlockFrame({ id, title, state, at, action, children }: {
  id: string;
  title: string;
  state: BlockState;
  at: number | null;
  action?: ReactNode;
  children?: ReactNode;
}) {
  const content = state === 'ready' || state === 'empty' || state === 'stale';
  return (
    <Card component="section" sx={{ mb: 3 }} data-testid={`overview-${id}`} data-state={state}
      aria-labelledby={`overview-${id}-title`} aria-busy={state === 'loading'}>
      <CardContent sx={{ pb: { xs: 1, sm: 2 } }}>
        <Box display="flex" justifyContent="space-between" alignItems="baseline" gap={2} flexWrap="wrap">
          <Typography variant="h6" component="h2" id={`overview-${id}-title`} sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
            {title}
          </Typography>
          <Box display="flex" alignItems="baseline" gap={2}>
            {at !== null && state !== 'stale' && (
              <Typography variant="caption" color="text.secondary" data-testid={`overview-${id}-checked`}>
                {`Checked ${clockText(at)}.`}
              </Typography>
            )}
            {action}
          </Box>
        </Box>
        {state === 'loading' && <CircularProgress size={20} aria-label={`Loading ${title}`} sx={{ mt: 1 }} />}
        {(state === 'denied' || state === 'unavailable' || state === 'failed') && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }} data-testid={`overview-${id}-state`}>
            {BLOCK_TEXT[state]}
          </Typography>
        )}
        {state === 'stale' && at !== null && (
          <Typography variant="body2" color="warning.main" sx={{ mt: 1 }} data-testid={`overview-${id}-state`}>
            {staleText(at)}
          </Typography>
        )}
        {content && children}
      </CardContent>
    </Card>
  );
}

function LinkLine({ testId, destination, onNavigate, children }: {
  testId: string;
  destination: string;
  onNavigate?: Navigate;
  children: ReactNode;
}) {
  return (
    <Button data-testid={testId} color="inherit" size="small" disabled={!onNavigate} onClick={() => go(onNavigate, destination)}
      endIcon={<ChevronRightIcon fontSize="small" />}
      sx={{ justifyContent: 'space-between', textAlign: 'left', textTransform: 'none', px: 1, mx: -1, width: '100%' }}>
      <Box component="span" sx={{ flex: 1, display: 'flex', flexDirection: 'column' }}>{children}</Box>
    </Button>
  );
}

export function DoingContent({ used, results, days, newInstall, connect, canSetUp, onNavigate }: {
  used: UsedCapability[];
  results: ResultLine[] | null;
  days: number;
  newInstall: boolean;
  connect: ConnectNext[];
  canSetUp: boolean;
  onNavigate?: Navigate;
}) {
  if (newInstall) {
    return (
      <Box sx={{ mt: 1 }} data-testid="overview-doing-new">
        <Typography variant="body2">No fax provider is set up yet, so Faxbot has nothing to improve so far.</Typography>
        {connect.map((entry) => (
          <LinkLine key={entry.address} testId={`overview-connect-${entry.address}`} destination={entry.address} onNavigate={onNavigate}>
            <Typography variant="body2" component="span" data-part="title">{`Connect in ${entry.label}`}</Typography>
            <Typography variant="caption" component="span" color="text.secondary" data-part="detail">
              {`Then these work right away: ${entry.capabilities.join(', ')}.`}
            </Typography>
          </LinkLine>
        ))}
        {canSetUp && onNavigate && (
          <Button variant="contained" size="small" sx={{ mt: 1 }} onClick={() => onNavigate('setup')}>Set up a fax provider</Button>
        )}
      </Box>
    );
  }
  if (used.length === 0 && results !== null && results.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }} data-testid="overview-doing-empty">
        {`Nothing Faxbot can improve acted on your faxes in the last ${days} days, so there are no results yet.`}
      </Typography>
    );
  }
  return (
    <Box sx={{ mt: 1 }}>
      {used.length > 0 && (
        <>
          <Typography variant="subtitle2" component="h3" color="text.secondary">{`Used here in the last ${days} days`}</Typography>
          {used.map((item) => (
            <LinkLine key={item.key} testId={`overview-used-${item.key}`} destination={item.address} onNavigate={onNavigate}>
              <Typography variant="body2" component="span" data-part="title">{item.name}</Typography>
              <Typography variant="caption" component="span" color="text.secondary" data-part="detail">{item.sentence}</Typography>
            </LinkLine>
          ))}
        </>
      )}
      {results === null ? (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }} data-testid="overview-results-state">
          The results could not be read. Select Refresh to try again.
        </Typography>
      ) : results.length > 0 && (
        <>
          <Typography variant="subtitle2" component="h3" color="text.secondary" sx={{ mt: 1 }}>Results, each in its own unit</Typography>
          {results.map((line) => (
            <Box key={line.unit} data-testid={`overview-result-${line.unit}`} sx={{ mb: 0.5 }}>
              <Typography variant="body2" component="span" fontWeight="bold">{`${line.label}: `}</Typography>
              <Typography variant="body2" component="span">
                {line.sentence ?? line.parts.map((part) => `${part.label} ${part.value}`).join(' · ')}
              </Typography>
            </Box>
          ))}
          <Link component="button" variant="body2" onClick={() => go(onNavigate, 'savings/results')} disabled={!onNavigate}>
            See how each was counted
          </Link>
        </>
      )}
    </Box>
  );
}

export function NextContent({ items, onNavigate }: { items: Improvement[]; onNavigate?: Navigate }) {
  if (items.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }} data-testid="overview-next-empty">
        Nothing to suggest right now. Every capability, and what each needs, is on the Capabilities page.
      </Typography>
    );
  }
  return (
    <Box sx={{ mt: 1 }}>
      {items.map((item) => (
        <LinkLine key={item.key} testId={`overview-next-${item.key}`} destination={item.destination} onNavigate={onNavigate}>
          <Typography variant="body2" component="span">
            <span data-part="title">{item.title}</span>
            <Chip component="span" size="small" variant="outlined" label={item.kindLabel} sx={{ ml: 1 }}
              color={item.kind === 'now' ? 'success' : 'default'} data-testid="overview-next-kind" />
          </Typography>
          <Typography variant="caption" component="span" color="text.secondary" data-part="detail">{item.sentence}</Typography>
        </LinkLine>
      ))}
    </Box>
  );
}

export function EverydayContent({ lines, onSendFax, onNavigate, children }: {
  lines: EverydayLine[];
  onSendFax?: () => void;
  onNavigate?: Navigate;
  children?: ReactNode;
}) {
  return (
    <Box sx={{ mt: 1 }}>
      {lines.map((line) => (
        <LinkLine key={line.key} testId={`overview-everyday-${line.key}`} destination={line.destination} onNavigate={onNavigate}>
          <Typography variant="body2" component="span" fontWeight="bold" data-part="title">{line.label}</Typography>
          <Typography variant="body2" component="span" color="text.secondary" data-part="detail">{line.text}</Typography>
        </LinkLine>
      ))}
      {onSendFax && (
        <Button variant="contained" size="small" startIcon={<SendIcon />} sx={{ mt: 1, mb: 2 }} onClick={onSendFax}
          data-testid="overview-everyday-send">
          Send a fax
        </Button>
      )}
      {children}
    </Box>
  );
}
