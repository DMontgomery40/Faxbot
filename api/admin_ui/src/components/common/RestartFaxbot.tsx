import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, CircularProgress } from '@mui/material';
import AdminAPIClient, { RestartNotAllowed, isForbidden } from '../../api/client';

// One restart flow for every screen with saved changes that wait for a
// restart: ask the API to restart, wait until it has gone away and answers
// again, then let the screen reload what it shows.

export type RestartPhase = 'idle' | 'requesting' | 'waiting' | 'back' | 'not_allowed' | 'failed' | 'slow';

export const RESTART_BY_HAND = 'Run docker compose restart api on the server.';
export const RESTARTED = 'Faxbot restarted and is using the saved settings.';

const PHASE_TEXT: Partial<Record<RestartPhase, string>> = {
  requesting: 'Restarting Faxbot…',
  waiting: 'Restarting Faxbot…',
  back: RESTARTED,
  not_allowed: `Restarting from the console is turned off here. ${RESTART_BY_HAND}`,
  failed: `Faxbot could not be restarted from here. ${RESTART_BY_HAND}`,
  slow: 'Faxbot has not come back yet. Check the server, then reload this page.',
};

export interface RestartOptions {
  // Called once the API answers again, to reload settings or status.
  onBack?: () => void | Promise<void>;
  pollMs?: number;
  timeoutMs?: number;
  // The API exits half a second after accepting the request. If no check saw
  // it gone, it counts as back only after this long.
  settleMs?: number;
}

export function useRestartFaxbot(client: AdminAPIClient, options: RestartOptions = {}) {
  const { onBack, pollMs = 1000, timeoutMs = 90000, settleMs = 8000 } = options;
  const [phase, setPhase] = useState<RestartPhase>('idle');
  const epoch = useRef(0);
  const back = useRef(onBack);
  back.current = onBack;

  useEffect(() => () => { epoch.current += 1; }, []);

  const restart = useCallback(async () => {
    const mine = ++epoch.current;
    setPhase('requesting');
    try {
      const result = await client.restart();
      if (result?.ok !== true) throw new Error('The restart request was not accepted.');
    } catch (error) {
      if (mine === epoch.current) setPhase(error instanceof RestartNotAllowed || isForbidden(error) ? 'not_allowed' : 'failed');
      return;
    }
    if (mine !== epoch.current) return;
    setPhase('waiting');
    const started = Date.now();
    let wentAway = false;
    while (mine === epoch.current) {
      await new Promise((resolve) => setTimeout(resolve, pollMs));
      if (mine !== epoch.current) return;
      const serving = await client.isServing();
      if (mine !== epoch.current) return;
      const elapsed = Date.now() - started;
      if (!serving) {
        wentAway = true;
      } else if (wentAway || elapsed >= settleMs) {
        try {
          await back.current?.();
        } catch {
          // The screen shows its own reload problem; the restart itself worked.
        }
        if (mine === epoch.current) setPhase('back');
        return;
      }
      if (elapsed >= timeoutMs) {
        setPhase('slow');
        return;
      }
    }
  }, [client, pollMs, timeoutMs, settleMs]);

  return { phase, busy: phase === 'requesting' || phase === 'waiting', text: PHASE_TEXT[phase] ?? null, restart };
}

interface RestartNoticeProps extends RestartOptions {
  client: AdminAPIClient;
  // The one sentence that says why a restart is needed.
  text: string;
  // False when this person may not restart the server; the sentence then says how to do it by hand.
  canRestart?: boolean;
}

// "Restart Faxbot to apply …" with a Restart now button, then the outcome.
export function RestartNotice({ client, text, canRestart = true, ...options }: RestartNoticeProps) {
  const { phase, busy, text: outcome, restart } = useRestartFaxbot(client, options);
  const done = phase === 'back';
  const problem = phase === 'failed' || phase === 'not_allowed' || phase === 'slow';
  return (
    <Alert severity={done ? 'success' : problem ? 'error' : 'warning'} data-testid="restart-notice"
      action={canRestart && !done && phase !== 'not_allowed' ? (
        <Button color="inherit" size="small" onClick={() => { void restart(); }} disabled={busy}
          startIcon={busy ? <CircularProgress size={14} color="inherit" /> : undefined}>
          Restart now
        </Button>
      ) : undefined}>
      {done ? outcome : <>
        {text}{!canRestart && ` ${RESTART_BY_HAND}`}
        {outcome && <Box component="span" sx={{ display: 'block', mt: 0.5 }} role="status">{outcome}</Box>}
      </>}
    </Alert>
  );
}

export default RestartNotice;
