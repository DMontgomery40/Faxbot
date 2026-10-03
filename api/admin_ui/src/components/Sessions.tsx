import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material';
import DevicesIcon from '@mui/icons-material/Devices';
import AdminAPIClient, { isNotAvailable } from '../api/client';
import type { AccessSession, AccessUser, AuthMe } from '../api/types';
import { formatServerTime } from '../api/time';
import {
  ConfirmDialog,
  EmptyState,
  ErrorBanner,
  LoadStateView,
  ScreenHeader,
  SelectField,
  StatusChip,
  loadFailure,
  type LoadState,
} from './access/AccessViews';

const SOURCE_LABEL: Record<string, string> = { password: 'Password', key: 'API key', bootstrap: 'Installation key' };

function sessionStatus(session: AccessSession): { label: string; tone: 'success' | 'default' | 'info' } {
  if (session.revoked_at) return { label: 'Ended', tone: 'default' };
  if (session.current) return { label: 'This session', tone: 'info' };
  return { label: 'Active', tone: 'success' };
}

export default function Sessions({ client, me }: { client: AdminAPIClient; me: AuthMe }) {
  const canSeeOthers = me.permissions.includes('sessions:read');
  const canEndOthers = me.permissions.includes('sessions:revoke');
  const [principalId, setPrincipalId] = useState(me.principal.id);
  const [people, setPeople] = useState<AccessUser[]>([]);
  const [sessions, setSessions] = useState<AccessSession[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  // The older own-sessions route, used until /access/sessions is available.
  const [ownRoute, setOwnRoute] = useState(false);
  const [state, setState] = useState<LoadState>('loading');
  const [loadingMore, setLoadingMore] = useState(false);
  const [ending, setEnding] = useState<AccessSession | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const viewingSelf = principalId === me.principal.id;

  useEffect(() => {
    if (!canSeeOthers) return;
    client.listUsers({ kind: 'all', limit: 200 }).then((page) => setPeople(page.items)).catch(() => setPeople([]));
  }, [client, canSeeOthers]);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      const page = await client.listSessions(viewingSelf ? {} : { principal_id: principalId });
      setSessions(page.items);
      setCursor(page.next_cursor);
      setOwnRoute(false);
      setState('ready');
    } catch (failure) {
      if (viewingSelf && isNotAvailable(failure)) {
        try {
          const own = await client.listOwnSessions();
          setSessions(own.items.map((item) => ({ ...item, principal: { id: me.principal.id, display_name: me.principal.display_name } })));
          setCursor(null);
          setOwnRoute(true);
          setState('ready');
          return;
        } catch (inner) {
          setState(loadFailure(inner));
          return;
        }
      }
      setState(loadFailure(failure));
    }
  }, [client, principalId, viewingSelf, me.principal]);

  useEffect(() => { void load(); }, [load]);

  const loadMore = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await client.listSessions({ principal_id: viewingSelf ? undefined : principalId, cursor });
      setSessions((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
    } catch (failure) {
      setError(failure);
    } finally {
      setLoadingMore(false);
    }
  };

  const reload = async () => {
    setError(null);
    await client.me().catch(() => undefined);
    await load();
  };

  const end = async () => {
    if (!ending) return;
    setBusy(true);
    setError(null);
    try {
      if (ownRoute) await client.revokeOwnSession(ending.session_id);
      else await client.revokeSession(ending.session_id);
      setEnding(null);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const peopleOptions = useMemo(() => {
    const options = people.filter((p) => p.kind !== 'bootstrap').map((p) => ({ value: p.id, label: p.display_name }));
    if (!options.some((o) => o.value === me.principal.id)) options.unshift({ value: me.principal.id, label: me.principal.display_name });
    return options;
  }, [people, me.principal]);

  const canEnd = (session: AccessSession) => !session.revoked_at && (viewingSelf || canEndOthers);

  return (
    <Box>
      <ScreenHeader title="Sessions" subtitle={viewingSelf ? 'Where you are signed in. End any session you do not recognize.' : 'Where this person is signed in.'}
        onRefresh={() => void reload()} busy={state === 'loading'} />

      {canSeeOthers && (
        <Box maxWidth={360} mb={2}>
          <SelectField label="Person" value={principalId} onChange={(value) => setPrincipalId(value || me.principal.id)} options={peopleOptions} />
        </Box>
      )}

      {!ending && <ErrorBanner error={error} onReload={() => void reload()} onClose={() => setError(null)} />}

      {state !== 'ready' ? <LoadStateView state={state} onRetry={() => void load()} />
        : sessions.length === 0 ? <EmptyState icon={<DevicesIcon />} title="No sessions" text="Nobody is signed in with this account." />
        : (
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Signed in with</TableCell>
                  <TableCell>Started</TableCell>
                  <TableCell>Last active</TableCell>
                  <TableCell>Expires</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell align="right">Actions</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {sessions.map((session) => {
                  const status = sessionStatus(session);
                  return (
                    <TableRow key={session.session_id} hover>
                      <TableCell>
                        <Typography variant="body2">{SOURCE_LABEL[session.source_kind] ?? session.source_kind}</Typography>
                        {!viewingSelf && <Typography variant="caption" color="text.secondary">{session.principal.display_name}</Typography>}
                      </TableCell>
                      <TableCell>{formatServerTime(session.created_at)}</TableCell>
                      <TableCell>{formatServerTime(session.last_used_at)}</TableCell>
                      <TableCell>{formatServerTime(session.expires_at)}</TableCell>
                      <TableCell><StatusChip label={status.label} tone={status.tone} /></TableCell>
                      <TableCell align="right">
                        {canEnd(session) && (
                          <Button size="small" color="error" onClick={() => { setError(null); setEnding(session); }}>
                            {session.current ? 'Sign out here' : 'End session'}
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
        )}

      {state === 'ready' && cursor && (
        <Box textAlign="center" mt={2}>
          <Button onClick={() => void loadMore()} disabled={loadingMore} startIcon={loadingMore ? <CircularProgress size={16} /> : undefined}>
            Load more
          </Button>
        </Box>
      )}

      <ConfirmDialog open={ending !== null} title={ending?.current ? 'Sign out of this session?' : 'End this session?'}
        text={ending?.current ? 'You will return to the sign-in screen.' : 'Whoever is using this session is signed out immediately.'}
        confirmLabel={ending?.current ? 'Sign out' : 'End session'} danger busy={busy} error={error}
        onConfirm={() => void end()} onCancel={() => setEnding(null)} onReload={() => void reload()} />
    </Box>
  );
}
