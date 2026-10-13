// The live outbound board at the top of Overview, and its compact form on Sent: what is sending now, what waits and
// why, what needs you, how today went, how full the lines are, and how each big send is going. Both read the one
// shared poll (useOutboundNow); neither polls on its own.
import { useEffect, useState, type ReactNode } from 'react';
import {
  Alert, Box, Button, ButtonBase, Card, CardContent, Chip, CircularProgress, Collapse, LinearProgress, Tooltip,
  Typography, useTheme,
} from '@mui/material';
import { alpha, type Theme } from '@mui/material/styles';
import AdminAPIClient from '../../api/client';
import type {
  NeedsYouItem, OutboundGroup, OutboundLine, OutboundNow,
} from '../../api/outboundTypes';
import type { AdminDestination } from '../../navigation';
import { rulesApiFor, type Hold, type HoldDecision } from '../ProviderRulesApi';
import { RefuseDialog, SendAnywayDialog } from '../ProviderRulesHeld';
import { DeliveryError, Notice } from '../delivery/shared';
import {
  ACTION_LABEL, BOARD_TEXT, busyText, checkedText, decisionNotice, deliveredOfText, failedText, faxName, goesOutText,
  inUseText, LEGEND, lineFull, needsYouLine, notChecked, onTheLineText, pagesText, routeText, sendingLine, shownOfText,
  sinceText, todayLine, untilText, waitingLine, waitingMoreText,
} from './outboundText';
import { useOutboundNow, type OutboundNowView } from './useOutboundNow';

type TileKey = 'sending' | 'waiting' | 'needs_you' | 'today';

const numberSx = {
  fontWeight: 600,
  lineHeight: 1.1,
  fontVariantNumeric: 'tabular-nums',
  letterSpacing: '-0.01em',
};

// Two by two on a phone, four across from a tablet up; one per row only below 360 px.
const tileGridSx = {
  display: 'grid',
  gap: { xs: 1, sm: 1.5 },
  gridTemplateColumns: { xs: 'repeat(2, minmax(0, 1fr))', md: 'repeat(4, minmax(0, 1fr))' },
  '@media (max-width: 359.95px)': { gridTemplateColumns: 'minmax(0, 1fr)' },
};

function Tile({ id, title, value, unit, line, unread, urgent = false, selected = false, compact = false, onClick,
  controls, hint }: {
  id: TileKey;
  title: string;
  value: number;
  unit?: string;
  line: string;
  // The server could not read this section: never a 0 in its place.
  unread: boolean;
  urgent?: boolean;
  selected?: boolean;
  compact?: boolean;
  onClick?: () => void;
  // The list this tile opens, for a tile that expands.
  controls?: string;
  // What selecting the tile does, when that is not opening its own list.
  hint?: string;
}) {
  const theme = useTheme();
  const tint = alpha(theme.palette.error.main, theme.palette.mode === 'dark' ? 0.14 : 0.06);
  const content = (
    <>
      <Typography component="span" variant="subtitle2" color={urgent ? 'error' : 'text.secondary'} sx={{ display: 'block' }}>
        {title}
      </Typography>
      {unread ? (
        <Typography component="span" variant="body2" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}
          data-testid={`tile-${id}-not-checked`}>
          {BOARD_TEXT.notChecked}
        </Typography>
      ) : (
        <>
          <Box component="span" sx={{ display: 'flex', alignItems: 'baseline', gap: 0.75 }}>
            <Typography component="span" data-testid={`tile-${id}-count`} color={urgent ? 'error' : 'text.primary'}
              sx={{ ...numberSx, fontSize: compact ? { xs: '1.5rem', sm: '1.75rem' } : { xs: '1.875rem', sm: '2.25rem' } }}>
              {value}
            </Typography>
            {unit && <Typography component="span" variant="body2" color="text.secondary">{unit}</Typography>}
          </Box>
          <Typography component="span" variant="body2" color="text.secondary" data-testid={`tile-${id}-line`}
            sx={{ display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden',
              overflowWrap: 'anywhere' }}>
            {line}
          </Typography>
        </>
      )}
    </>
  );
  const sx = {
    display: 'flex', flexDirection: 'column', alignItems: 'stretch', justifyContent: 'flex-start', gap: 0.5,
    textAlign: 'left', minWidth: 0, p: compact ? 1.5 : 2, borderRadius: 1, border: 1,
    borderColor: selected ? 'primary.main' : urgent ? 'error.main' : 'divider',
    boxShadow: selected ? `inset 0 0 0 1px ${urgent ? theme.palette.error.main : theme.palette.primary.main}` : 'none',
    bgcolor: urgent ? tint : 'transparent',
  };
  if (!onClick) return <Box sx={sx} data-testid={`tile-${id}`} data-urgent={String(urgent)}>{content}</Box>;
  const button = (
    <ButtonBase focusRipple onClick={onClick} aria-expanded={controls ? selected : undefined} aria-controls={controls}
      data-testid={`tile-${id}`} data-urgent={String(urgent)}
      sx={{ ...sx, transition: 'border-color 0.15s', '&:hover': { borderColor: urgent ? 'error.dark' : 'primary.main' },
        '&.Mui-focusVisible': { outline: `2px solid ${theme.palette.primary.main}`, outlineOffset: 2 } }}>
      {content}
    </ButtonBase>
  );
  return hint ? <Tooltip title={hint} arrow>{button}</Tooltip> : button;
}

function tiles(board: OutboundNow) {
  return {
    sending: { title: BOARD_TEXT.sendingTitle, value: board.sending.count, line: sendingLine(board),
      unread: notChecked(board, 'sending') },
    waiting: { title: BOARD_TEXT.waitingTitle, value: board.waiting.count, line: waitingLine(board),
      unread: notChecked(board, 'waiting') },
    needs_you: { title: BOARD_TEXT.needsYouTitle, value: board.needs_you.count, line: needsYouLine(board),
      unread: notChecked(board, 'needs_you') },
    today: { title: BOARD_TEXT.todayTitle, value: board.today.delivered, unit: BOARD_TEXT.delivered,
      line: todayLine(board), unread: notChecked(board, 'today') },
  };
}

// -- lines ------------------------------------------------------------------------------------------------

// One slot per call the carrier allows: outgoing calls, then incoming ones, then free slots.
function LineSlots({ line }: { line: OutboundLine }) {
  const theme = useTheme();
  const full = lineFull(line);
  const fill = full ? theme.palette.warning.main : theme.palette.primary.main;
  const slots = line.limit ?? line.in_use;
  const label = `${line.label}: ${inUseText(line)}`;
  if (slots === 0) return null;
  if (slots > 12) {
    const value = line.limit ? Math.min(100, (line.in_use / line.limit) * 100) : 0;
    return (
      <LinearProgress variant="determinate" value={value} aria-label={label} color={full ? 'warning' : 'primary'}
        sx={{ height: 10, borderRadius: 5 }} />
    );
  }
  const outgoing = Math.max(0, line.in_use - line.incoming);
  return (
    <Box role="img" aria-label={label} sx={{ display: 'flex', gap: 0.5 }} data-testid="line-slots">
      {Array.from({ length: slots }, (_, index) => (
        <Box key={index} data-slot={index < outgoing ? 'out' : index < line.in_use ? 'in' : 'free'}
          sx={{ flex: 1, maxWidth: 72, height: 10, borderRadius: 5,
            bgcolor: index < outgoing ? fill : index < line.in_use ? alpha(fill, 0.45) : 'action.selected' }} />
      ))}
    </Box>
  );
}

function LinesMeter({ board, compact = false }: { board: OutboundNow; compact?: boolean }) {
  if (notChecked(board, 'lines')) {
    return (
      <Box sx={{ mt: compact ? 1.5 : 2 }} data-testid="outbound-lines">
        <Typography variant="subtitle2" component="h3" color="text.secondary">{BOARD_TEXT.linesTitle}</Typography>
        <Typography variant="body2" color="text.secondary" data-testid="lines-not-checked">{BOARD_TEXT.notChecked}</Typography>
      </Box>
    );
  }
  if (board.lines.length === 0) return null;
  return (
    <Box sx={{ mt: compact ? 1.5 : 2 }} data-testid="outbound-lines">
      <Typography variant="subtitle2" component="h3" color="text.secondary">{BOARD_TEXT.linesTitle}</Typography>
      <Box sx={{ display: 'grid', gap: compact ? 1 : 1.5, mt: 0.5,
        gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: board.lines.length > 1 ? 'repeat(2, minmax(0, 1fr))' : 'minmax(0, 1fr)' } }}>
        {board.lines.map((line) => (
          <Box key={line.key} data-testid="outbound-line" data-full={String(lineFull(line))}>
            <Box display="flex" justifyContent="space-between" alignItems="baseline" gap={1} flexWrap="wrap" mb={0.5}>
              <Typography variant="body2" fontWeight={600}>
                {line.label}
                {lineFull(line) && <Chip component="span" label={BOARD_TEXT.lineFull} color="warning" size="small"
                  sx={{ ml: 1, height: 20 }} />}
              </Typography>
              <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }} data-testid="line-in-use">
                {inUseText(line)}
              </Typography>
            </Box>
            <LineSlots line={line} />
            {!compact && (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>{line.sentence}</Typography>
            )}
          </Box>
        ))}
      </Box>
    </Box>
  );
}

// -- the lists a tile opens ---------------------------------------------------------------------------------

function Row({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <Box data-testid={testId} sx={{ py: 1.25, borderTop: 1, borderColor: 'divider', '&:first-of-type': { borderTop: 0 } }}>
      {children}
    </Box>
  );
}

function Empty({ text }: { text: string }) {
  return <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>{text}</Typography>;
}

function SendingList({ board }: { board: OutboundNow }) {
  // Only while this list is open does the time on the line tick, once a second, in this browser.
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, []);
  if (board.sending.count === 0) return <Empty text={BOARD_TEXT.sendingEmpty} />;
  return (
    <>
      {board.sending.items.map((fax) => (
        <Row key={fax.id} testId="sending-item">
          <Box display="flex" justifyContent="space-between" gap={2} flexWrap="wrap">
            <Typography variant="body2" fontWeight={600}>{faxName(fax)}</Typography>
            {fax.started_at && (
              <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }} data-testid="on-the-line">
                {onTheLineText(fax.started_at, now)}
              </Typography>
            )}
          </Box>
          <Typography variant="body2" color="text.secondary">
            {[fax.recipient ? fax.to : null, pagesText(fax), routeText(fax)].filter(Boolean).join(', ')}
          </Typography>
        </Row>
      ))}
      {board.sending.count > board.sending.items.length && (
        <Typography variant="caption" color="text.secondary">{shownOfText(board.sending.items.length, board.sending.count)}</Typography>
      )}
    </>
  );
}

function WaitingList({ board, now }: { board: OutboundNow; now: number }) {
  const { waiting } = board;
  if (waiting.count === 0) return <Empty text={BOARD_TEXT.waitingEmpty} />;
  const reasons = waiting.reasons.filter((reason) => reason.count > 0).sort((a, b) => b.count - a.count);
  return (
    <>
      <Typography variant="subtitle2" component="h4" sx={{ mt: 0.5 }}>{BOARD_TEXT.reasonsTitle}</Typography>
      <Box component="ul" sx={{ listStyle: 'none', p: 0, m: 0, mb: 1.5 }}>
        {reasons.map((reason) => (
          <Box component="li" key={reason.key} data-testid="waiting-reason"
            sx={{ display: 'flex', gap: 1.5, alignItems: 'baseline', py: 0.5 }}>
            <Typography variant="body2" fontWeight={600} sx={{ minWidth: '2.5ch', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
              {reason.count}
            </Typography>
            <Typography variant="body2">{reason.sentence}</Typography>
          </Box>
        ))}
        {waiting.more > 0 && (
          <Box component="li" sx={{ py: 0.5 }} data-testid="waiting-more">
            <Typography variant="body2" color="text.secondary">{waitingMoreText(waiting.more)}</Typography>
          </Box>
        )}
      </Box>
      <Typography variant="subtitle2" component="h4">{BOARD_TEXT.firstInLine}</Typography>
      {waiting.items.map((fax) => (
        <Row key={fax.id} testId="waiting-item">
          <Box display="flex" justifyContent="space-between" gap={2} flexWrap="wrap">
            <Typography variant="body2" fontWeight={600}>{faxName(fax)}</Typography>
            {untilText(fax.until, now) && (
              <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }} data-testid="waiting-until">
                {untilText(fax.until, now)}
              </Typography>
            )}
          </Box>
          <Typography variant="body2" color="text.secondary">{fax.sentence}</Typography>
        </Row>
      ))}
      {waiting.count > waiting.items.length && (
        <Typography variant="caption" color="text.secondary">{shownOfText(waiting.items.length, waiting.count)}</Typography>
      )}
    </>
  );
}

// A held fax as the existing held-fax dialogs and requests read it; they use its id, version and number.
function heldFax(item: NeedsYouItem): Hold {
  return {
    id: item.hold!.id, job_id: item.id, kind: item.kind === 'approval' || item.kind === 'no_route' ? item.kind : 'window',
    to_number: faxName(item), pages: item.pages, sender_name: null, requested_at: item.since, until: item.until ?? null,
    reason: item.sentence, can_decide: true, version: item.hold!.version,
  };
}

function NeedsYouList({ client, board, now, onOpenSentFax, onChanged }: {
  client: AdminAPIClient;
  board: OutboundNow;
  now: number;
  onOpenSentFax?: (jobId: string) => void;
  onChanged: () => void;
}) {
  const api = rulesApiFor(client);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [refusing, setRefusing] = useState<{ item: NeedsYouItem; hold: Hold } | null>(null);
  const [sendingAnyway, setSendingAnyway] = useState<{ item: NeedsYouItem; hold: Hold } | null>(null);

  const decide = async (action: () => Promise<HoldDecision>, message: string) => {
    setBusy(true);
    setError(null);
    try {
      const decision = await action();
      setNotice(decision?.sentence || message);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  // The accounts a fax may still go by are read when asked for, with the hold's latest version.
  const sendByAccount = async (item: NeedsYouItem) => {
    setBusy(true);
    setError(null);
    try {
      const { holds } = await api.holds();
      const hold = holds.find((candidate) => candidate.id === item.hold?.id);
      if (hold) setSendingAnyway({ item, hold: { ...hold, to_number: faxName(item) } });
      else {
        setNotice(BOARD_TEXT.noLongerHeld);
        onChanged();
      }
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const run = (item: NeedsYouItem, action: string) => {
    if (action === 'settle' || !item.hold) {
      onOpenSentFax?.(item.id);
      return;
    }
    if (action === 'approve') void decide(() => api.approve(heldFax(item)), decisionNotice('approve', item));
    else if (action === 'check_again') void decide(() => api.checkAgain(heldFax(item)), decisionNotice('check_again', item));
    else if (action === 'refuse') setRefusing({ item, hold: heldFax(item) });
    else if (action === 'send_by_account') void sendByAccount(item);
  };

  const { needs_you: needsYou } = board;
  return (
    <>
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      {needsYou.count === 0 && <Empty text={BOARD_TEXT.needsYouEmpty} />}
      {needsYou.items.map((item) => {
        // An item the server offers no decision on opens in Sent; so does an unknown result (settled there).
        const actions = item.actions.filter((action) => ACTION_LABEL[action]);
        const shown = actions.length > 0 ? actions : ['settle'];
        return (
          <Row key={item.id} testId="needs-you-item">
            <Box display="flex" justifyContent="space-between" gap={2} flexWrap="wrap">
              <Typography variant="body2" fontWeight={600}>{faxName(item)}</Typography>
              <Typography variant="caption" color="text.secondary">{sinceText(item.since, now)}</Typography>
            </Box>
            <Typography variant="body2" color="text.secondary">{item.sentence}</Typography>
            {item.kind === 'window' && goesOutText(item.until, now) && (
              <Typography variant="caption" color="text.secondary" display="block">{goesOutText(item.until, now)}</Typography>
            )}
            <Box display="flex" gap={1} flexWrap="wrap" sx={{ mt: 1 }}>
              {shown.map((action) => (
                <Button key={action} size="small" disabled={busy || (action === 'settle' && !onOpenSentFax)}
                  variant={action === 'approve' || action === 'send_by_account' ? 'contained' : action === 'settle' ? 'outlined' : 'text'}
                  color={action === 'refuse' ? 'error' : 'primary'}
                  aria-label={`${ACTION_LABEL[action]}: the fax to ${faxName(item)}`}
                  onClick={() => run(item, action)}>
                  {ACTION_LABEL[action]}
                </Button>
              ))}
            </Box>
          </Row>
        );
      })}
      {needsYou.count > needsYou.items.length && (
        <Typography variant="caption" color="text.secondary">{shownOfText(needsYou.items.length, needsYou.count)}</Typography>
      )}
      <SendAnywayDialog hold={sendingAnyway?.hold ?? null} onClose={() => setSendingAnyway(null)} onSend={(account) => {
        const chosen = sendingAnyway!;
        const label = chosen.hold.options?.find((option) => option.account === account)?.label ?? account;
        setSendingAnyway(null);
        void decide(() => api.approve(chosen.hold, account), decisionNotice('send_by_account', chosen.item, label));
      }} />
      <RefuseDialog hold={refusing?.hold ?? null} onClose={() => setRefusing(null)} onRefuse={(reason) => {
        const chosen = refusing!;
        setRefusing(null);
        void decide(() => api.refuse(chosen.hold, reason), decisionNotice('refuse', chosen.item));
      }} />
    </>
  );
}

function TodayList({ board }: { board: OutboundNow }) {
  const { today } = board;
  const failures = [...today.failures].filter((failure) => failure.count > 0).sort((a, b) => b.count - a.count);
  return (
    <>
      <Box display="flex" gap={4} flexWrap="wrap" sx={{ py: 1 }}>
        {([
          ['delivered', today.delivered, BOARD_TEXT.todayDelivered, 'success.main'],
          ['failed', today.failed, BOARD_TEXT.todayFailed, today.failed > 0 ? 'error.main' : 'text.primary'],
          ['accepted', today.accepted, BOARD_TEXT.todayAccepted, 'text.primary'],
        ] as const).map(([key, value, label, color]) => (
          <Box key={key}>
            <Typography sx={{ ...numberSx, fontSize: '1.5rem' }} color={color} data-testid={`today-${key}`}>{value}</Typography>
            <Typography variant="caption" color="text.secondary">{label}</Typography>
          </Box>
        ))}
      </Box>
      {failures.length === 0 ? <Empty text={BOARD_TEXT.failuresEmpty} /> : (
        <>
          <Typography variant="subtitle2" component="h4" sx={{ mt: 1 }}>{BOARD_TEXT.failuresTitle}</Typography>
          <Box component="ol" sx={{ listStyle: 'none', p: 0, m: 0 }}>
            {failures.map((failure) => (
              <Box component="li" key={failure.key} data-testid="today-failure"
                sx={{ display: 'flex', gap: 1.5, alignItems: 'baseline', py: 0.5 }}>
                <Typography variant="body2" fontWeight={600} color="error"
                  sx={{ minWidth: '2.5ch', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                  {failure.count}
                </Typography>
                <Typography variant="body2">{failure.label}</Typography>
              </Box>
            ))}
          </Box>
        </>
      )}
    </>
  );
}

// -- big sends ----------------------------------------------------------------------------------------------

function segmentColor(theme: Theme, key: (typeof LEGEND)[number]['key']): string {
  switch (key) {
    case 'delivered': return theme.palette.success.main;
    case 'sending': return theme.palette.primary.main;
    case 'waiting': return theme.palette.mode === 'dark' ? theme.palette.grey[600] : theme.palette.grey[400];
    case 'needs_you': return theme.palette.warning.main;
    default: return theme.palette.error.main;
  }
}

function GroupBar({ group }: { group: OutboundGroup }) {
  const theme = useTheme();
  const total = Math.max(group.total, 1);
  const parts = LEGEND.filter((part) => group[part.key] > 0);
  return (
    <Box data-testid="outbound-group">
      <Box display="flex" justifyContent="space-between" alignItems="baseline" gap={1} flexWrap="wrap" mb={0.5}>
        <Typography variant="body2" fontWeight={600}>{group.who}</Typography>
        <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }} data-testid="group-delivered">
          {deliveredOfText(group)}
        </Typography>
      </Box>
      <Box role="img" aria-label={`${group.who}: ${parts.map((part) => `${group[part.key]} ${part.label.toLowerCase()}`).join(', ')}`}
        sx={{ display: 'flex', height: 12, borderRadius: 6, overflow: 'hidden', bgcolor: 'action.selected' }}>
        {parts.map((part) => (
          <Box key={part.key} data-segment={part.key} data-count={group[part.key]}
            style={{ width: `${(group[part.key] / total) * 100}%` }} sx={{ bgcolor: segmentColor(theme, part.key) }} />
        ))}
      </Box>
      <Box display="flex" gap={1.5} flexWrap="wrap" sx={{ mt: 0.5 }}>
        {parts.filter((part) => part.key !== 'delivered').map((part) => (
          <Box key={part.key} display="flex" alignItems="center" gap={0.5}>
            <Box sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: segmentColor(theme, part.key) }} />
            <Typography variant="caption" color="text.secondary">{`${group[part.key]} ${part.label.toLowerCase()}`}</Typography>
          </Box>
        ))}
      </Box>
    </Box>
  );
}

function Groups({ board }: { board: OutboundNow }) {
  if (notChecked(board, 'groups')) {
    return (
      <Box sx={{ mt: 2 }} data-testid="outbound-groups">
        <Typography variant="subtitle2" component="h3" color="text.secondary">{BOARD_TEXT.groupsTitle}</Typography>
        <Typography variant="body2" color="text.secondary">{BOARD_TEXT.notChecked}</Typography>
      </Box>
    );
  }
  if (board.groups.length === 0) return null;
  return (
    <Box sx={{ mt: 2 }} data-testid="outbound-groups">
      <Typography variant="subtitle2" component="h3" color="text.secondary">{BOARD_TEXT.groupsTitle}</Typography>
      <Box sx={{ display: 'grid', gap: 1.5, mt: 0.5,
        gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: board.groups.length > 1 ? 'repeat(2, minmax(0, 1fr))' : 'minmax(0, 1fr)' } }}>
        {board.groups.map((group) => <GroupBar key={group.who} group={group} />)}
      </Box>
    </Box>
  );
}

// The board's own time, and what the last poll found when it was not a fresh reading.
function Freshness({ view }: { view: OutboundNowView }) {
  if (view.at === null) return null;
  const text = view.state === 'busy' ? busyText(view.at) : view.state === 'failed' ? failedText(view.at) : checkedText(view.at);
  return (
    <Typography variant="caption" color={view.state === 'ok' ? 'text.secondary' : 'warning.main'} data-testid="outbound-checked">
      {text}
    </Typography>
  );
}

// No reading yet: loading, busy or failed, never a row of zeros.
function NoReading({ view }: { view: OutboundNowView }) {
  if (view.state === 'loading') return <CircularProgress size={20} aria-label={BOARD_TEXT.loading} sx={{ mt: 1 }} />;
  return (
    <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }} data-testid="outbound-no-reading">
      {view.state === 'busy' ? BOARD_TEXT.busyEmpty : BOARD_TEXT.failedEmpty}
    </Typography>
  );
}

const LIST_ID = (key: TileKey) => `outbound-list-${key}`;

export default function OutboundBoard({ client, onOpenSentFax, onNavigate }: {
  client: AdminAPIClient;
  // Opens one fax's details in Sent, where an unknown result is settled.
  onOpenSentFax?: (jobId: string) => void;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const view = useOutboundNow(client);
  const [open, setOpen] = useState<TileKey | null>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { setNow(Date.now()); }, [view.at]);
  if (view.state === 'denied' || view.state === 'unavailable') return null;
  const { board } = view;
  // Without a way to open one fax, Sent opens on the faxes whose result is unknown.
  const openFax = onOpenSentFax ?? (onNavigate ? () => onNavigate('faxes/sent?status=reconciliation_required') : undefined);
  const toggle = (key: TileKey) => setOpen((current) => (current === key ? null : key));
  const shown = board ? tiles(board) : null;
  return (
    <Card component="section" sx={{ mb: 3 }} data-testid="outbound-board" data-state={view.state}
      aria-labelledby="outbound-board-title" aria-busy={view.state === 'loading'}>
      <CardContent sx={{ pb: { xs: 1.5, sm: 2 } }}>
        <Box display="flex" justifyContent="space-between" alignItems="baseline" gap={2} flexWrap="wrap" mb={1.5}>
          <Typography variant="h6" component="h2" id="outbound-board-title" sx={{ fontSize: { xs: '1rem', sm: '1.25rem' } }}>
            {BOARD_TEXT.title}
          </Typography>
          <Freshness view={view} />
        </Box>
        {!board || !shown ? <NoReading view={view} /> : (
          <>
            {board.engine && (
              <Alert severity="warning" sx={{ mb: 2, borderRadius: 2 }} data-testid="outbound-engine">
                <Typography variant="body2">{board.engine.sentence}</Typography>
                {board.engine.fix && <Typography variant="body2" sx={{ mt: 0.5 }}>{board.engine.fix}</Typography>}
              </Alert>
            )}
            {!board.engine && notChecked(board, 'engine') && (
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }} data-testid="engine-not-checked">
                {BOARD_TEXT.engineNotChecked}
              </Typography>
            )}
            <Box sx={tileGridSx}>
              {(['sending', 'waiting', 'needs_you', 'today'] as TileKey[]).map((key) => (
                <Tile key={key} id={key} {...shown[key]} selected={open === key} controls={LIST_ID(key)}
                  urgent={key === 'needs_you' && !shown.needs_you.unread && board.needs_you.count > 0}
                  onClick={() => toggle(key)} />
              ))}
            </Box>
            <Collapse in={open !== null} unmountOnExit>
              {open && (
                <Box id={LIST_ID(open)} role="region" aria-label={shown[open].title} data-testid={LIST_ID(open)}
                  sx={{ mt: 1.5, px: { xs: 1.5, sm: 2 }, py: 1, borderRadius: 1, bgcolor: 'action.hover' }}>
                  {shown[open].unread ? <Empty text={BOARD_TEXT.notChecked} />
                    : open === 'sending' ? <SendingList board={board} />
                      : open === 'waiting' ? <WaitingList board={board} now={now} />
                        : open === 'needs_you' ? (
                          <NeedsYouList client={client} board={board} now={now} onOpenSentFax={openFax} onChanged={view.refresh} />
                        ) : <TodayList board={board} />}
                </Box>
              )}
            </Collapse>
            <LinesMeter board={board} />
            <Groups board={board} />
          </>
        )}
      </CardContent>
    </Card>
  );
}

// Sent's compact board: the four tiles and the lines. Waiting and Needs you show those faxes in the list below.
export function CompactOutboundBoard({ client, onShowWaiting, onShowNeedsYou }: {
  client: AdminAPIClient;
  onShowWaiting?: () => void;
  onShowNeedsYou?: () => void;
}) {
  const view = useOutboundNow(client);
  if (view.state === 'denied' || view.state === 'unavailable') return null;
  const { board } = view;
  const shown = board ? tiles(board) : null;
  return (
    <Box component="section" aria-label={BOARD_TEXT.title} data-testid="outbound-compact" data-state={view.state} sx={{ mb: 3 }}>
      {!board || !shown ? <NoReading view={view} /> : (
        <>
          <Box sx={tileGridSx}>
            {(['sending', 'waiting', 'needs_you', 'today'] as TileKey[]).map((key) => (
              <Tile key={key} id={key} {...shown[key]} compact
                urgent={key === 'needs_you' && !shown.needs_you.unread && board.needs_you.count > 0}
                onClick={key === 'waiting' ? onShowWaiting : key === 'needs_you' ? onShowNeedsYou : undefined}
                hint={key === 'waiting' && onShowWaiting ? BOARD_TEXT.showWaiting
                  : key === 'needs_you' && onShowNeedsYou ? BOARD_TEXT.showNeedsYou : undefined} />
            ))}
          </Box>
          <LinesMeter board={board} compact />
          <Box sx={{ mt: 1 }}><Freshness view={view} /></Box>
        </>
      )}
    </Box>
  );
}
